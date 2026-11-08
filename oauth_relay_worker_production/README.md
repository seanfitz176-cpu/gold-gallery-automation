# Stable Production OAuth callback relay

This is the Production counterpart of ../oauth_relay_worker -- deployed as its
own, separately named Cloudflare Worker (ebay-production-oauth-relay) so a fault
or misconfiguration here can never affect the Sandbox relay, and so the
Production RuName never points at the same Worker as the Sandbox RuName.

Same code, same security properties as the Sandbox relay (see its README for
the full explanation): no storage bindings, no outbound requests, no logging,
cannot select a user-controlled redirect target. It only ever redirects the
browser back to http://127.0.0.1:5000/oauth/callback on this computer.

Deploy with: `npx wrangler deploy` from inside this directory.
Once deployed, its callback URL is:
  https://ebay-production-oauth-relay.<your-subdomain>.workers.dev/oauth/callback
Register that exact URL as the Auth Accepted URL for your Production RuName in
the eBay Developer Portal. Its privacy policy page is served at /privacy on the
same host.

Keep the Worker name exactly `ebay-production-oauth-relay`, keep preview URLs and
observability disabled, and do not enable Cloudflare Access on its workers.dev
address.
