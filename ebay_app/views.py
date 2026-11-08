from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def h(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def layout(
    title: str,
    body: str,
    *,
    authenticated: bool,
    csrf_token: str,
    flashes: Sequence[Tuple[str, str]] = (),
) -> str:
    nav = ""
    if authenticated:
        nav = f"""
        <nav aria-label="Main navigation">
          <a href="/">Dashboard</a>
          <form method="post" action="/logout" class="inline">
            <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
            <button type="submit" class="link-button">Sign out</button>
          </form>
        </nav>"""
    messages = "".join(
        f'<div class="notice {h(kind)}" role="status">{h(message)}</div>'
        for kind, message in flashes
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)} · eBay Sandbox</title><link rel="stylesheet" href="/static/style.css"></head>
<body><header><div class="shell"><strong>eBay Sandbox Console</strong>{nav}</div></header>
<main class="shell" id="main"><h1>{h(title)}</h1>{messages}{body}</main>
<footer class="shell">Sandbox only · Every eBay change requires a separate confirmation.</footer></body></html>"""


def login_page(csrf_token: str, error: Optional[str] = None) -> str:
    alert = f'<div class="notice error" role="alert">{h(error)}</div>' if error else ""
    return layout(
        "Sign in",
        f"""{alert}<p>Use the local application password from <code>.env</code>.</p>
        <form method="post" action="/login" class="card narrow">
          <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
          <label for="password">Application password</label>
          <input id="password" name="password" type="password" required autocomplete="current-password">
          <button type="submit">Sign in</button>
        </form>""",
        authenticated=False,
        csrf_token=csrf_token,
    )


def privacy_page(environment: str = "Sandbox") -> str:
    body = f"""<section class="card"><p>This is a private eBay {h(environment)} development tool.</p>
    <ul><li>The current stage requests read access to the {h(environment)} user's identity and orders,
        plus Inventory permission for explicitly confirmed unpublished offers.</li>
      <li>Buyer identity, contact details, delivery addresses and payment details are not displayed or stored.</li>
      <li>Before confirmation, draft preparation performs no eBay write. Confirmation may create
        one Inventory item and one unpublished Offer in the {h(environment)}.</li>
      <li>It cannot publish, fulfil, refund, edit, withdraw or delete eBay data.</li>
      <li>OAuth refresh tokens are encrypted in a private database on the application owner's computer.</li>
      <li>Credentials and tokens are not displayed, sold or shared.</li>
      <li>Removing the local database removes this application's local token copy; revoke the grant in eBay to invalidate it.</li></ul>
    <p>This page is for {h(environment)} OAuth configuration and is not a substitute for a reviewed production privacy policy.</p></section>"""
    return layout(
        "Privacy notice",
        body,
        authenticated=False,
        csrf_token="",
    )


def oauth_handoff_page(
    authorization_url: str,
    csrf_token: str,
    feature_stage: str = "connection",
    environment: str = "Sandbox",
) -> str:
    if feature_stage == "drafts":
        permission = (
            f"read access to the {environment} user's identity and orders, plus Inventory "
            "permission to create explicitly confirmed unpublished offers"
        )
        boundary = (
            f"This permission can create {environment} Inventory records only after a separate "
            "local confirmation. Phase 5 contains no publish, delete, price, quantity, "
            "fulfilment or refund operation."
        )
    elif feature_stage == "orders":
        permission = (
            f"read-only access to the {environment} user's identity, Inventory listings "
            "and Fulfillment orders"
        )
        boundary = (
            "Buyer and delivery details are omitted. The application cannot fulfil, "
            "refund, create, publish, edit or delete eBay data."
        )
    elif feature_stage == "listings":
        permission = (
            f"read-only access to the {environment} user's identity and Inventory listings"
        )
        boundary = (
            "The application cannot create, publish, edit or delete listings, prices "
            "or quantities."
        )
    elif feature_stage == "setup":
        permission = (
            "the same Inventory-management access as before, plus Account-management "
            "access (sell.account), used only to create one merchant location and the "
            f"three business policies a {environment} offer needs"
        )
        boundary = (
            "This is a one-time setup step run separately, outside this web app, by "
            "the owner. It does not publish, fulfil, refund, price, or quantity-edit "
            "anything, and creates no listing."
        )
    elif feature_stage == "prices":
        permission = (
            f"read access to the {environment} user's identity and orders, plus "
            "Inventory permission to change an existing published offer's price"
        )
        boundary = (
            "This permission can change an existing offer's price only after a "
            "separate local confirmation that re-checks the live price first. It "
            "cannot publish, delete, change quantity, add images, or migrate a "
            "listing."
        )
    elif feature_stage == "quantities":
        permission = (
            f"read access to the {environment} user's identity and orders, plus "
            "Inventory permission to change an existing published offer's stock "
            "quantity"
        )
        boundary = (
            "This permission can change an existing offer's quantity only after a "
            "separate local confirmation that re-checks the live quantity first. "
            "It cannot publish, delete, change price, add images, or migrate a "
            "listing."
        )
    elif feature_stage == "images":
        permission = (
            f"read access to the {environment} user's identity and orders, plus "
            "Inventory permission to add one image URL to an existing published "
            "offer's Inventory item"
        )
        boundary = (
            "This permission can add one externally-hosted image URL only after a "
            "separate local confirmation. It cannot publish, delete, change price, "
            "change quantity, or migrate a listing."
        )
    elif feature_stage == "migration":
        permission = (
            f"read access to the {environment} user's identity and orders, plus "
            "Inventory permission to migrate one existing classic listing into "
            "Inventory API management"
        )
        boundary = (
            "This permission is used only by the standalone migrate_listing.py "
            "script, run separately by the owner from a terminal, after a typed "
            "Item ID confirmation. There is no dashboard route that uses it -- it "
            "does not publish, delete, change price, change quantity, or add "
            "images."
        )
    elif feature_stage == "publish":
        permission = (
            f"read access to the {environment} user's identity and orders, plus "
            "Inventory permission to publish existing unpublished offers"
        )
        boundary = (
            "This permission is used only by the standalone bulk_publish_listings.py "
            "script, run separately by the owner from a terminal, after a typed "
            "confirmation. There is no dashboard route that uses it -- it does not "
            "delete, change price, change quantity, or add images."
        )
    else:
        permission = f"read-only access to the {environment} user's identity"
        boundary = "No listing, order, price or inventory API is enabled."
    body = f"""<div class="notice info"><strong>Ready to open eBay {h(environment)}.</strong>
    This step requests {h(permission)}.</div>
    <section class="card narrow">
      <p>Continue to eBay's own sign-in and consent page. {h(boundary)}</p>
      <p><a class="button" href="{h(authorization_url)}" rel="noreferrer">Continue to eBay {h(environment)}</a></p>
      <p><a href="/">Cancel and return to the dashboard</a></p>
    </section>"""
    return layout(
        f"Continue to eBay {environment}",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def dashboard_page(
    status: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    csrf_token: str,
    flashes: Sequence[Tuple[str, str]],
    environment: str = "Sandbox",
    feature_stage: str = "full",
    runtime_mode: str = "local",
    listing_permission: bool = False,
    order_permission: bool = False,
    draft_permission: bool = False,
    setup_permission: bool = False,
    price_permission: bool = False,
    quantity_permission: bool = False,
    image_permission: bool = False,
    migration_permission: bool = False,
    publish_permission: bool = False,
) -> str:
    if feature_stage in {
        "connection",
        "account",
        "listings",
        "orders",
        "drafts",
        "setup",
        "prices",
        "quantities",
        "images",
        "migration",
        "publish",
    }:
        required_permission = {
            "drafts": draft_permission,
            "orders": order_permission,
            "setup": setup_permission,
            "prices": price_permission,
            "quantities": quantity_permission,
            "images": image_permission,
            "migration": migration_permission,
            "publish": publish_permission,
        }.get(feature_stage, listing_permission)
        if (
            feature_stage
            in {
                "listings", "orders", "drafts", "setup", "prices", "quantities",
                "images", "migration", "publish",
            }
            and status.get("connected")
            and status.get("verified")
            and not required_permission
        ):
            permission_name = {
                "drafts": f"Inventory management for unpublished {environment} offers",
                "orders": "Fulfillment order access",
                "setup": f"Account management for one-time {environment} setup",
                "prices": f"Inventory management for {environment} price changes",
                "quantities": f"Inventory management for {environment} quantity changes",
                "images": f"Inventory management for {environment} listing images",
                "migration": f"Inventory management for {environment} listing migration",
                "publish": f"Inventory management for publishing {environment} listings",
            }.get(feature_stage, "Inventory access")
            button_label = {
                "drafts": f"Authorize unpublished {environment} drafts",
                "orders": "Authorize read-only orders",
                "setup": f"Authorize one-time {environment} setup",
                "prices": f"Authorize {environment} price changes",
                "quantities": f"Authorize {environment} quantity changes",
                "images": f"Authorize {environment} image changes",
                "migration": f"Authorize {environment} listing migration",
                "publish": f"Authorize {environment} listing publishing",
            }.get(feature_stage, "Authorize read-only listings")
            permission_qualifier = (
                ""
                if feature_stage
                in {"drafts", "setup", "prices", "quantities", "images", "migration", "publish"}
                else "read-only "
            )
            connection = f"""<div class="notice warning"><strong>One additional permission is required.</strong>
            The existing verified grant is still stored. Authorize {h(permission_qualifier)}{h(permission_name)}
            with the same {h(environment)} seller; the old grant is replaced only after eBay succeeds.</div>
            <form method="post" action="/connect">
              <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
              <button type="submit">{h(button_label)}</button>
            </form>"""
        elif status.get("verified"):
            verified_at = status.get("verified_at")
            checked = ""
            if isinstance(verified_at, int):
                checked = datetime.fromtimestamp(verified_at, timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            connection = f"""<div class="notice success"><strong>Connection verified.</strong>
            OAuth refresh and the read-only Identity request both succeeded{f' at {h(checked)}' if checked else ''}.</div>
            <form method="post" action="/connection/verify">
              <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
              <button type="submit" class="secondary">Verify again</button>
            </form>"""
        elif status.get("connected"):
            connection = f"""<div class="notice warning"><strong>Authorized, but not verified yet.</strong>
            eBay returned a grant. Run the read-only connection test to confirm it still works.</div>
            <form method="post" action="/connection/verify">
              <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
              <button type="submit">Verify {h(environment)} connection</button>
            </form>"""
        else:
            connection = f"""<div class="notice warning"><strong>Not authorized yet.</strong>
            Start eBay's {h(environment)} consent flow. No listing or inventory API will be called.</div>
            <form method="post" action="/connect">
              <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
              <button type="submit">Authorize eBay {h(environment)}</button>
            </form>"""

        if feature_stage == "drafts":
            account_link = (
                '<a class="button secondary" href="/account">View account information</a>'
                if status.get("verified")
                else ""
            )
            listing_link = (
                '<a class="button secondary" href="/listings">View active listings</a>'
                if status.get("verified") and listing_permission
                else ""
            )
            order_link = (
                '<a class="button secondary" href="/orders">View recent orders</a>'
                if status.get("verified") and order_permission
                else ""
            )
            draft_link = (
                '<a class="button" href="/drafts/new">Prepare an unpublished offer</a>'
                if status.get("verified") and draft_permission
                else ""
            )
            action_rows = "".join(
                f'<tr><td><a href="/actions/{h(item["action_id"])}">Unpublished offer</a></td>'
                f'<td><span class="badge {h(item["status"])}">{h(item["status"])}</span></td>'
                f'<td>{h(item.get("error_message") or "")}</td></tr>'
                for item in actions
                if item.get("kind") == "create_draft"
            ) or '<tr><td colspan="3">No completed draft confirmations yet.</td></tr>'
            recent_confirmations = f"""<section><h2>Recent completed confirmations</h2>
            <div class="table-wrap"><table>
              <thead><tr><th>Action</th><th>Status</th><th>Message</th></tr></thead>
              <tbody>{action_rows}</tbody></table></div></section>"""
            boundary = f"""<section class="card"><h2>Phase 5 safety boundary</h2>
            <p>Preparing a draft validates the form, performs a read-only SKU check and shows
            an exact review. It does not change eBay.</p>
            <p>Only the separate one-use confirmation can create two persistent {h(environment)} records:
            one Inventory item and one <strong>unpublished</strong> Offer. It never publishes a
            live listing.</p>
            <div class="actions">{draft_link}{order_link}{listing_link}{account_link}</div>
            <p>Publishing, price changes, quantity updates, images, deletion, fulfilment and
            refunds remain locked.</p></section>{recent_confirmations}"""
            title = f"Unpublished {environment} offers"
        elif feature_stage == "prices":
            account_link = (
                '<a class="button secondary" href="/account">View account information</a>'
                if status.get("verified")
                else ""
            )
            listing_link = (
                '<a class="button" href="/listings">View listings and change a price</a>'
                if status.get("verified") and listing_permission
                else ""
            )
            order_link = (
                '<a class="button secondary" href="/orders">View recent orders</a>'
                if status.get("verified") and order_permission
                else ""
            )
            draft_link = (
                '<a class="button secondary" href="/drafts/new">Prepare an unpublished offer</a>'
                if status.get("verified") and draft_permission
                else ""
            )
            action_labels = {"create_draft": "Unpublished offer", "change_price": "Price change"}
            action_rows = "".join(
                f'<tr><td><a href="/actions/{h(item["action_id"])}">'
                f'{h(action_labels.get(item.get("kind"), "Action"))}</a></td>'
                f'<td><span class="badge {h(item["status"])}">{h(item["status"])}</span></td>'
                f'<td>{h(item.get("error_message") or "")}</td></tr>'
                for item in actions
                if item.get("kind") in action_labels
            ) or '<tr><td colspan="3">No completed confirmations yet.</td></tr>'
            recent_confirmations = f"""<section><h2>Recent completed confirmations</h2>
            <div class="table-wrap"><table>
              <thead><tr><th>Action</th><th>Status</th><th>Message</th></tr></thead>
              <tbody>{action_rows}</tbody></table></div></section>"""
            boundary = f"""<section class="card"><h2>Phase 6 safety boundary</h2>
            <p>Changing a price validates the form, re-reads the live Offer, and shows an
            exact review comparing the current and new price. It does not change eBay.</p>
            <p>Only the separate one-use confirmation re-verifies the Offer once more and
            applies the new price to one already-published {h(environment)} Offer. It never creates,
            publishes, deletes, or changes anything else.</p>
            <p>Start a price change from the listings page, where each published Offer has
            its own "Review price" control.</p>
            <div class="actions">{listing_link}{draft_link}{order_link}{account_link}{'<a class="button secondary" href="/price-limits">Manage price safety limits</a>' if status.get('verified') else ''}</div>
            <p>Publishing new listings, quantity updates, images, deletion, fulfilment and
            refunds remain locked.</p></section>{recent_confirmations}"""
            title = f"Edit a {environment} listing price"
        elif feature_stage == "quantities":
            account_link = (
                '<a class="button secondary" href="/account">View account information</a>'
                if status.get("verified")
                else ""
            )
            listing_link = (
                '<a class="button" href="/listings">View listings and change a price or quantity</a>'
                if status.get("verified") and listing_permission
                else ""
            )
            order_link = (
                '<a class="button secondary" href="/orders">View recent orders</a>'
                if status.get("verified") and order_permission
                else ""
            )
            draft_link = (
                '<a class="button secondary" href="/drafts/new">Prepare an unpublished offer</a>'
                if status.get("verified") and draft_permission
                else ""
            )
            action_labels = {
                "create_draft": "Unpublished offer",
                "change_price": "Price change",
                "change_quantity": "Quantity change",
            }
            action_rows = "".join(
                f'<tr><td><a href="/actions/{h(item["action_id"])}">'
                f'{h(action_labels.get(item.get("kind"), "Action"))}</a></td>'
                f'<td><span class="badge {h(item["status"])}">{h(item["status"])}</span></td>'
                f'<td>{h(item.get("error_message") or "")}</td></tr>'
                for item in actions
                if item.get("kind") in action_labels
            ) or '<tr><td colspan="3">No completed confirmations yet.</td></tr>'
            recent_confirmations = f"""<section><h2>Recent completed confirmations</h2>
            <div class="table-wrap"><table>
              <thead><tr><th>Action</th><th>Status</th><th>Message</th></tr></thead>
              <tbody>{action_rows}</tbody></table></div></section>"""
            boundary = f"""<section class="card"><h2>Phase 7 safety boundary</h2>
            <p>Changing a quantity validates the form, re-reads the live Offer, and shows an
            exact review comparing the current and new quantity. It does not change eBay.</p>
            <p>Only the separate one-use confirmation re-verifies the Offer once more and
            applies the new quantity to one already-published {h(environment)} Offer. It never creates,
            publishes, deletes, or changes anything else.</p>
            <p>Start a quantity change from the listings page, where each published Offer has
            its own "Review quantity" control.</p>
            <div class="actions">{listing_link}{draft_link}{order_link}{account_link}{'<a class="button secondary" href="/price-limits">Manage price safety limits</a>' if status.get('verified') else ''}</div>
            <p>Publishing new listings, images, deletion, fulfilment and refunds remain
            locked.</p></section>{recent_confirmations}"""
            title = f"Edit a {environment} listing quantity"
        elif feature_stage == "images":
            account_link = (
                '<a class="button secondary" href="/account">View account information</a>'
                if status.get("verified")
                else ""
            )
            listing_link = (
                '<a class="button" href="/listings">View listings and change a price, quantity or image</a>'
                if status.get("verified") and listing_permission
                else ""
            )
            order_link = (
                '<a class="button secondary" href="/orders">View recent orders</a>'
                if status.get("verified") and order_permission
                else ""
            )
            draft_link = (
                '<a class="button secondary" href="/drafts/new">Prepare an unpublished offer</a>'
                if status.get("verified") and draft_permission
                else ""
            )
            action_labels = {
                "create_draft": "Unpublished offer",
                "change_price": "Price change",
                "change_quantity": "Quantity change",
                "add_image": "Image added",
            }
            action_rows = "".join(
                f'<tr><td><a href="/actions/{h(item["action_id"])}">'
                f'{h(action_labels.get(item.get("kind"), "Action"))}</a></td>'
                f'<td><span class="badge {h(item["status"])}">{h(item["status"])}</span></td>'
                f'<td>{h(item.get("error_message") or "")}</td></tr>'
                for item in actions
                if item.get("kind") in action_labels
            ) or '<tr><td colspan="3">No completed confirmations yet.</td></tr>'
            recent_confirmations = f"""<section><h2>Recent completed confirmations</h2>
            <div class="table-wrap"><table>
              <thead><tr><th>Action</th><th>Status</th><th>Message</th></tr></thead>
              <tbody>{action_rows}</tbody></table></div></section>"""
            boundary = f"""<section class="card"><h2>Phase 8 safety boundary</h2>
            <p>Adding an image validates the form, performs a live read-only check of the
            image address (reachable, an actual image, under the size limit), re-reads the
            live Inventory item, and shows an exact review. It does not change eBay and it
            never downloads or stores the image itself -- only its address is sent to eBay.</p>
            <p>Only the separate one-use confirmation re-verifies the Offer and the listing's
            current images once more and appends exactly one image URL to one already-published
            {h(environment)} listing. It never creates, publishes, deletes, or changes anything else.</p>
            <p>Start adding an image from the listings page, where each published Offer has
            its own "Review image" control.</p>
            <div class="actions">{listing_link}{draft_link}{order_link}{account_link}{'<a class="button secondary" href="/price-limits">Manage price safety limits</a>' if status.get('verified') else ''}</div>
            <p>Publishing new listings, deletion, fulfilment and refunds remain
            locked.</p></section>{recent_confirmations}"""
            title = f"Edit a {environment} listing image"
        elif feature_stage == "orders":
            account_link = (
                f'<p><a class="button secondary" href="/account">View {environment} account information</a></p>'
                if status.get("verified")
                else ""
            )
            listing_link = (
                '<p><a class="button secondary" href="/listings">View active listings</a></p>'
                if status.get("verified")
                and listing_permission
                else ""
            )
            order_link = (
                '<p><a class="button" href="/orders">View recent orders</a></p>'
                if status.get("verified")
                and order_permission
                else ""
            )
            boundary = f"""<section class="card"><h2>Phase 4 boundary</h2>
            <p>This stage can read a minimal order summary as well as the earlier account and
            active-listing summaries. Buyer identity, contact details and delivery addresses
            are omitted.</p>{order_link}{listing_link}{account_link}
            <p>Fulfilment, refunds, drafts, price changes, quantity changes, image uploads and
            every eBay mutation remain locked.</p></section>"""
            title = f"Recent {environment} orders"
        elif feature_stage == "listings":
            account_link = (
                f'<p><a class="button secondary" href="/account">View {environment} account information</a></p>'
                if status.get("verified")
                else ""
            )
            listing_link = (
                '<p><a class="button" href="/listings">View active listings</a></p>'
                if status.get("verified")
                and listing_permission
                else ""
            )
            boundary = f"""<section class="card"><h2>Phase 3 boundary</h2>
            <p>This stage can read the minimal account summary and active Inventory API listings.</p>
            {listing_link}{account_link}
            <p>Orders, drafts, price changes, quantity changes, image uploads and every eBay
            mutation remain locked.</p></section>"""
            title = f"Active {environment} listings"
        elif feature_stage == "account":
            account_link = (
                f'<p><a class="button" href="/account">View {environment} account information</a></p>'
                if status.get("verified")
                else ""
            )
            boundary = f"""<section class="card"><h2>Phase 2 boundary</h2>
            <p>This stage may display only account type and registration marketplace from
            <code>GET Identity /user/</code>.</p>{account_link}
            <p>Listings, orders, drafts, prices, quantities and images remain locked.</p></section>"""
            title = f"{environment} account"
        elif feature_stage == "setup":
            boundary = f"""<section class="card"><h2>One-time {h(environment)} setup</h2>
            <p>This stage only adds Account-management permission to the existing grant.
            It makes no eBay change by itself.</p>
            <p>Once authorized, run <code>python setup_business_policies.py</code> in a
            terminal to create the merchant location and business policies. That script
            shows its own exact plan and requires a typed confirmation before it changes
            anything in {h(environment)}.</p></section>"""
            title = f"One-time {environment} setup"
        elif feature_stage == "migration":
            boundary = f"""<section class="card"><h2>Listing migration</h2>
            <p>This stage only adds Inventory-management permission to the existing grant.
            It makes no eBay change by itself.</p>
            <p>Once authorized, run
            <code>python3 migrate_listing.py --item-id &lt;ID&gt;</code> in a terminal to
            migrate exactly one existing listing. That script prints eBay's full
            eligibility checklist and requires you to re-type the Item ID before it changes
            anything in {h(environment)}. There is no dashboard button for this --
            migration is a rare, one-off action, not a recurring change.</p></section>"""
            title = f"Listing migration ({environment})"
        elif feature_stage == "publish":
            boundary = f"""<section class="card"><h2>Publishing listings</h2>
            <p>This stage only adds Inventory-management permission to the existing grant.
            It makes no eBay change by itself.</p>
            <p>Once authorized, run
            <code>python3 bulk_publish_listings.py &lt;csv&gt;</code> in a terminal. That
            script re-reads every Offer, shows exactly which ones it will publish, honours
            the dry-run switch and the actions-per-run budget, and requires a typed
            confirmation before it changes anything in {h(environment)}. There is no
            dashboard button for this.</p></section>"""
            title = f"Publishing listings ({environment})"
        else:
            boundary = """<section class="card"><h2>Phase 1 boundary</h2>
            <p>Only OAuth token refresh and <code>GET Identity /user/</code> are enabled.</p>
            <p>Listings, orders, drafts, prices and inventory changes remain locked until this connection is verified.</p></section>"""
            title = f"{environment} connection"
        body = f"""{connection}{boundary}"""
        return layout(
            title,
            body,
            authenticated=True,
            csrf_token=csrf_token,
            flashes=flashes,
        )

    if status.get("connected"):
        connection = f"""<div class="notice success"><strong>Connected</strong>
        to an encrypted {h(environment)} seller grant.</div>"""
    else:
        connection = f"""<div class="notice warning"><strong>Not connected.</strong>
        <form class="inline" method="post" action="/connect">
          <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
          <button type="submit">Connect eBay {h(environment)}</button>
        </form></div>"""
    action_rows = "".join(
        f"<tr><td><a href=\"/actions/{h(item['action_id'])}\">{h(item['kind'])}</a></td>"
        f"<td><span class=\"badge {h(item['status'])}\">{h(item['status'])}</span></td>"
        f"<td>{h(item['error_message'] or '')}</td></tr>"
        for item in actions
    ) or '<tr><td colspan="3">No confirmed changes yet.</td></tr>'
    body = f"""{connection}
    <div class="grid"><section class="card"><h2>Read from {h(environment)}</h2>
      <p><a class="button" href="/listings">View Inventory listings</a></p>
      <p><a class="button secondary" href="/orders">View recent orders</a></p></section>
    <section class="card"><h2>Prepare a change</h2><p>Every eBay mutation has a separate review page.</p>
      <a class="button" href="/drafts/new">Create an unpublished draft</a></section></div>
    <section><h2>Recent completed actions</h2><div class="table-wrap"><table>
      <thead><tr><th>Action</th><th>Status</th><th>Message</th></tr></thead><tbody>{action_rows}</tbody>
    </table></div></section>"""
    return layout(
        "Dashboard",
        body,
        authenticated=True,
        csrf_token=csrf_token,
        flashes=flashes,
    )


def account_page(summary: Mapping[str, str], csrf_token: str) -> str:
    details = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value)}</dd>"
        for label, value in (
            ("Environment", summary["environment"]),
            ("Account type", summary["account_type"]),
            ("Registration marketplace", summary["registration_marketplace"]),
            ("Retrieved", summary["retrieved_at"]),
        )
    )
    body = f"""<div class="notice success"><strong>Live read-only Identity check succeeded.</strong>
    No selling API or mutation was called.</div>
    <section class="card narrow"><dl>{details}</dl></section>
    <section class="card"><h2>Privacy boundary</h2>
      <p>This page intentionally omits eBay identifiers, username, status, names, addresses,
      email addresses, phone numbers and the raw API response. Nothing from this profile is cached.</p>
    </section>"""
    return layout(
        f"{summary['environment']} account information",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def active_listings_page(
    listings: Sequence[Mapping[str, Any]],
    environment: str,
    retrieved_at: str,
    csrf_token: str,
) -> str:
    rows = "".join(
        f"<tr><td>{h(item['title'])}</td><td><code>{h(item['sku'])}</code></td>"
        f"<td><code>{h(item['listing_id'])}</code></td>"
        f"<td><span class=\"badge {h(str(item['listing_state']).lower().replace(' ', '-'))}\">{h(item['listing_state'])}</span></td>"
        f"<td>{h(item['marketplace'])}</td>"
        f"<td>{h(item['currency'])} {h(item['price_value'])}</td>"
        f"<td>{h(item['available_quantity'])}</td></tr>"
        for item in listings
    ) or '<tr><td colspan="7">No active Inventory API listings were found.</td></tr>'
    body = f"""<div class="notice success"><strong>Live read-only listing check succeeded.</strong>
    No eBay data was changed. Environment: {h(environment)}. Retrieved: {h(retrieved_at)}.</div>
    <div class="notice info">Only listings managed through eBay's Inventory API appear here.
    Older Seller Hub or Trading API listings may not appear until they are migrated.</div>
    <div class="table-wrap"><table><thead><tr><th>Title</th><th>SKU</th>
      <th>Listing ID</th><th>Status</th><th>Marketplace</th><th>Price</th><th>Available qty</th>
    </tr></thead><tbody>{rows}</tbody></table></div>
    <section class="card"><h2>Privacy and safety boundary</h2>
      <p>This page omits descriptions, item specifics, images, policy and location data,
      offer IDs and raw eBay responses. It contains no edit controls.</p></section>"""
    return layout(
        f"Active {environment} listings",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def _field(
    name: str,
    label: str,
    value: str,
    errors: Mapping[str, str],
    *,
    field_type: str = "text",
    required: bool = True,
    help_text: str = "",
) -> str:
    error = errors.get(name)
    described = f'{name}-error' if error else f'{name}-help' if help_text else ""
    classes = "field invalid" if error else "field"
    required_attribute = "required" if required else ""
    invalid_attribute = 'aria-invalid="true"' if error else ""
    described_attribute = f'aria-describedby="{h(described)}"' if described else ""
    error_html = (
        f'<div class="field-error" id="{h(name)}-error">{h(error)}</div>' if error else ""
    )
    help_html = (
        f'<div class="help" id="{h(name)}-help">{h(help_text)}</div>' if help_text else ""
    )
    return f"""<div class="{classes}"><label for="{h(name)}">{h(label)}</label>
    <input id="{h(name)}" name="{h(name)}" type="{h(field_type)}" value="{h(value)}"
      {required_attribute} {invalid_attribute} {described_attribute}>
    {error_html}{help_html}</div>"""


def draft_form_page(
    csrf_token: str,
    values: Optional[Mapping[str, str]] = None,
    errors: Optional[Mapping[str, str]] = None,
    environment: str = "Sandbox",
) -> str:
    values = values or {}
    errors = errors or {}
    summary = ""
    if errors:
        summary = '<div class="notice error" role="alert"><strong>No eBay changes were made.</strong> Correct the form below.</div>'
    fields = "".join(
        [
            _field(
                "sku",
                "SKU label",
                values.get("sku", ""),
                errors,
                help_text=(
                    "Use 1–21 letters, numbers, periods, underscores or hyphens. "
                    f"A unique {environment} suffix is added and shown on the review screen."
                ),
            ),
            _field("title", "Title", values.get("title", ""), errors),
            _field(
                "condition",
                "Condition code",
                values.get("condition", "NEW"),
                errors,
                help_text="For example NEW, USED_GOOD or USED_ACCEPTABLE.",
            ),
            _field("category_id", "eBay category ID", values.get("category_id", ""), errors),
            _field("quantity", "Quantity", values.get("quantity", "1"), errors, field_type="number"),
            _field("price", "Price", values.get("price", ""), errors, field_type="text"),
        ]
    )
    description_error = (
        f'<div class="field-error">{h(errors.get("description"))}</div>'
        if errors.get("description")
        else ""
    )
    aspects_error = (
        f'<div class="field-error">{h(errors.get("aspects"))}</div>'
        if errors.get("aspects")
        else ""
    )
    body = f"""{summary}<div class="notice info"><strong>This first screen does not change eBay.</strong>
    It validates the details, checks that the SKU is unused, and then shows an exact review.</div>
    <form method="post" action="/drafts/review" class="card">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}">{fields}
      <div class="field"><label for="description">Description</label>
        <textarea id="description" name="description" rows="7" required>{h(values.get('description', ''))}</textarea>
        <div class="help">Plain text only. Image upload remains locked until Phase 8.</div>
        {description_error}</div>
      <div class="field"><label for="aspects">Item aspects JSON (optional)</label>
        <textarea id="aspects" name="aspects" rows="4">{h(values.get('aspects', ''))}</textarea>
        <div class="help">Example: <code>{{"Brand":["Example"]}}</code></div>
        {aspects_error}</div>
      <button type="submit">Review without changing eBay</button>
    </form>"""
    return layout("New unpublished draft", body, authenticated=True, csrf_token=csrf_token)


def draft_review_page(
    token: str,
    draft: Mapping[str, Any],
    csrf_token: str,
    offer_settings: Optional[Mapping[str, Any]] = None,
    environment: str = "Sandbox",
) -> str:
    details = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value)}</dd>"
        for label, value in (
            ("SKU", draft["sku"]),
            ("Title", draft["title"]),
            ("Condition", draft["condition"]),
            ("Category", draft["category_id"]),
            ("Quantity", draft["quantity"]),
            ("Price", f"{draft['currency']} {draft['price']}"),
        )
    )
    settings = offer_settings or {}
    settings_html = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value or 'Not configured')}</dd>"
        for label, value in (
            ("Environment", settings.get("environment")),
            ("Marketplace", settings.get("marketplace_id")),
            ("Currency", settings.get("currency")),
            ("Listing format", settings.get("format")),
            ("Listing duration", settings.get("listing_duration")),
            ("Inventory location", settings.get("merchant_location_key")),
            ("Fulfilment policy", settings.get("fulfillment_policy_id")),
            ("Payment policy", settings.get("payment_policy_id")),
            ("Return policy", settings.get("return_policy_id")),
        )
    )
    aspects_json = json.dumps(draft.get("aspects", {}), indent=2, sort_keys=True)
    body = f"""<div class="notice warning" role="status"><strong>No eBay change has been made yet.</strong>
    Check every value below. The separate button creates two persistent {h(environment)} records:
    one Inventory item and one unpublished Offer. The displayed SKU includes the app's unique
    {h(environment)} suffix, which protects existing seller records from accidental replacement.</div>
    <section class="card"><dl>{details}</dl><h2>Description</h2><p class="prewrap">{h(draft['description'])}</p>
      <h2>Aspects</h2><pre>{h(aspects_json)}</pre>
      <h2>Configured Offer settings</h2><dl>{settings_html}</dl></section>
    <div class="notice info"><strong>The Offer will not be live.</strong>
    This application has no Phase 5 route that calls eBay's publish operation. Images, price edits,
    quantity edits and deletion also remain locked.</div>
    <div class="actions"><form method="post" action="/actions/confirm">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}"><input type="hidden" name="action_token" value="{h(token)}">
      <button type="submit" class="danger">Create unpublished {h(environment)} offer</button></form>
    <form method="post" action="/actions/cancel"><input type="hidden" name="csrf_token" value="{h(csrf_token)}">
      <input type="hidden" name="action_token" value="{h(token)}"><button type="submit" class="secondary">Cancel</button></form></div>"""
    return layout("Review draft", body, authenticated=True, csrf_token=csrf_token)


def draft_action_page(
    action: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    csrf_token: str,
    environment: str = "Sandbox",
) -> str:
    """Render only the small, allowlisted Phase 5 reconciliation summary."""

    status = str(action.get("status", "unknown"))
    messages = {
        "succeeded": (
            "success",
            f"Unpublished {environment} offer created and verified. It is not a live listing.",
        ),
        "failed": (
            "error",
            "The draft stopped before an eBay write was confirmed.",
        ),
        "partial": (
            "warning",
            "The Inventory item was created, but the unpublished Offer was not confirmed.",
        ),
        "unknown": (
            "warning",
            f"The result is uncertain. Check this SKU in eBay {environment} before doing "
            "anything else.",
        ),
        "cancelled": ("info", "The confirmation was cancelled before execution."),
        "expired": ("info", "The confirmation expired before execution."),
    }
    notice_class, message = messages.get(
        status,
        ("warning", "This draft action requires review before another attempt."),
    )
    result = action.get("result")
    safe_result = result if isinstance(result, Mapping) else {}
    result_fields = (
        ("SKU", "sku"),
        ("Offer ID", "offer_id"),
        ("Offer status", "offer_status"),
        ("Marketplace", "marketplace_id"),
        ("Inventory item created", "inventory_created"),
        ("Inventory item verified", "inventory_verified"),
        ("Offer created", "offer_created"),
        ("Verified as unpublished", "verified_unpublished"),
        ("Stopped at", "failure_stage"),
        ("Safe error code", "safe_code"),
        ("Reconciliation required", "reconciliation_required"),
    )
    result_rows = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value)}</dd>"
        for label, key in result_fields
        if (value := safe_result.get(key)) is not None
    ) or "<dt>Result</dt><dd>No additional public details were stored.</dd>"

    event_labels = {
        "confirmed": "Confirmation accepted",
        "inventory_request_started": "Inventory request started",
        "inventory_created": "Inventory item created",
        "inventory_verified": "Inventory item verified against the review",
        "offer_request_started": "Unpublished Offer request started",
        "offer_created": "Unpublished Offer created",
        "verified_unpublished": "Offer verified as unpublished",
        "inventory_request_failed": "Inventory request stopped",
        "offer_request_failed": "Offer request stopped",
        "verification_failed": "Unpublished verification failed",
        "reconciliation_required": "Manual reconciliation required",
        "terminal_result": "Final result recorded",
    }
    event_rows = "".join(
        "<tr>"
        f"<td>{h(event_labels.get(str(event.get('event_type')), 'Workflow update'))}</td>"
        f"<td>{h(event.get('terminal_status') or '')}</td>"
        "</tr>"
        for event in events
    ) or '<tr><td colspan="2">No workflow events were recorded.</td></tr>'
    guidance = ""
    if status in {"partial", "unknown"} or safe_result.get("reconciliation_required") is True:
        guidance = f"""<div class="notice warning"><strong>Do not retry this SKU yet.</strong>
        First inspect the SKU and any Offer in eBay {h(environment)}. The application deliberately does
        not delete, roll back or retry an uncertain write.</div>"""
    body = f"""<div class="notice {h(notice_class)}" role="status"><strong>Status:
    {h(status)}</strong> {h(message)}</div>{guidance}
    <section class="card"><h2>Safe result summary</h2><dl>{result_rows}</dl></section>
    <section><h2>Workflow record</h2><div class="table-wrap"><table>
      <thead><tr><th>Step</th><th>Outcome</th></tr></thead><tbody>{event_rows}</tbody>
    </table></div></section>
    <p><a class="button secondary" href="/">Back to dashboard</a></p>"""
    return layout(
        f"Unpublished {environment} offer result",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def listings_page(
    listings: Sequence[Mapping[str, Any]],
    csrf_token: str,
    feature_stage: str = "prices",
) -> str:
    rows = []
    for entry in listings:
        item = entry.get("inventory") or {}
        offer = entry.get("offer") or {}
        product = item.get("product") or {}
        price = (offer.get("pricingSummary") or {}).get("price") or {}
        status = offer.get("status") or "INVENTORY_ONLY"
        edit_forms = ""
        if status == "PUBLISHED" and offer.get("offerId"):
            edit_forms = f"""<form method="post" action="/prices/review" class="compact-form">
              <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
              <input type="hidden" name="offer_id" value="{h(offer.get('offerId'))}">
              <input type="hidden" name="sku" value="{h(entry.get('sku'))}">
              <label><span class="sr-only">New price</span><input name="new_price" inputmode="decimal" placeholder="New price" required></label>
              <button type="submit">Review price</button></form>"""
            if feature_stage in {"quantities", "images"}:
                edit_forms += f"""<form method="post" action="/quantities/review" class="compact-form">
                  <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
                  <input type="hidden" name="offer_id" value="{h(offer.get('offerId'))}">
                  <input type="hidden" name="sku" value="{h(entry.get('sku'))}">
                  <label><span class="sr-only">New quantity</span><input name="new_quantity" inputmode="numeric" placeholder="New quantity" required></label>
                  <button type="submit">Review quantity</button></form>"""
            if feature_stage == "images":
                edit_forms += f"""<form method="post" action="/images/review" class="compact-form">
                  <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
                  <input type="hidden" name="offer_id" value="{h(offer.get('offerId'))}">
                  <input type="hidden" name="sku" value="{h(entry.get('sku'))}">
                  <label><span class="sr-only">Image URL</span><input name="image_url" type="url" inputmode="url" placeholder="https://images.example.com/photo.jpg" required></label>
                  <button type="submit">Review image</button></form>"""
        rows.append(
            f"<tr><td>{h(product.get('title') or 'Untitled')}</td><td><code>{h(entry.get('sku'))}</code></td>"
            f"<td><code>{h(offer.get('offerId') or '—')}</code></td><td><span class=\"badge {h(str(status).lower())}\">{h(status)}</span></td>"
            f"<td>{h(price.get('currency'))} {h(price.get('value'))}</td><td>{h(offer.get('availableQuantity', ''))}</td><td>{edit_forms}</td></tr>"
        )
    table_rows = "".join(rows) or '<tr><td colspan="7">No Inventory API records were found.</td></tr>'
    body = f"""<div class="notice info">This page shows Inventory API objects. Seller Hub or Trading listings may not appear until migrated.</div>
    <div class="table-wrap"><table><thead><tr><th>Title</th><th>SKU</th><th>Offer ID</th><th>Status</th><th>Price</th><th>Qty</th><th>Action</th></tr></thead>
    <tbody>{table_rows}</tbody></table></div>"""
    return layout("Listings", body, authenticated=True, csrf_token=csrf_token)


def recent_orders_page(
    orders: Sequence[Mapping[str, Any]],
    environment: str,
    retrieved_at: str,
    lookback_days: int,
    csrf_token: str,
) -> str:
    rows = "".join(
        f"<tr><td><code>{h(order['order_id'])}</code></td>"
        f"<td>{h(order['created_at'])}</td>"
        f"<td>{h(order['payment_status'])}</td>"
        f"<td>{h(order['fulfillment_status'])}</td>"
        f"<td>{h(order['cancellation_state'])}</td>"
        f"<td>{h(order['currency'])} {h(order['total_value'])}</td>"
        f"<td>{h(order['line_item_count'])}</td></tr>"
        for order in orders
    ) or f'<tr><td colspan="7">No recent {h(environment)} orders were found.</td></tr>'
    body = f"""<div class="notice success"><strong>Live read-only order check succeeded.</strong>
    No eBay data was changed. Environment: {h(environment)}. Retrieved: {h(retrieved_at)}.</div>
    <div class="notice info">This page shows completed-checkout orders created within the
    last {h(lookback_days)} days. An empty table is a successful result when the {h(environment)} seller
    has no recent test purchases.</div>
    <div class="table-wrap"><table><thead><tr><th>Order ID</th><th>Created</th>
      <th>Payment</th><th>Fulfilment</th><th>Cancellation</th><th>Total</th><th>Line items</th>
    </tr></thead><tbody>{rows}</tbody></table></div>
    <section class="card"><h2>Privacy and safety boundary</h2>
      <p>This page omits buyer identity, usernames, email, phone, delivery addresses,
      checkout notes, item titles and SKUs, payment details, tax data, tracking data and raw
      eBay responses. It contains no fulfilment, refund or edit controls.</p></section>"""
    return layout(
        f"Recent {environment} orders",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def price_limits_page(
    limits: Sequence[Mapping[str, Any]],
    default_change_percent: str,
    csrf_token: str,
    values: Optional[Mapping[str, str]] = None,
    errors: Optional[Mapping[str, str]] = None,
    flashes: Sequence[Tuple[str, str]] = (),
) -> str:
    values = values or {}
    errors = errors or {}
    rows = "".join(
        f"<tr><td><code>{h(limit['sku'])}</code></td>"
        f"<td>{h(limit['floor_currency'])} {h(limit['floor_price'])}</td>"
        f"<td>{h(limit['max_change_percent']) + '%' if limit.get('max_change_percent') else f'Uses global default ({h(default_change_percent)}%)'}</td>"
        f"<td>{h(limit['updated_at'])}</td>"
        f"<td><form method=\"post\" action=\"/price-limits/delete\" class=\"inline\">"
        f"<input type=\"hidden\" name=\"csrf_token\" value=\"{h(csrf_token)}\">"
        f"<input type=\"hidden\" name=\"sku\" value=\"{h(limit['sku'])}\">"
        f"<button type=\"submit\" class=\"link-button\">Remove</button></form></td></tr>"
        for limit in limits
    ) or '<tr><td colspan="5">No SKUs have a floor price set yet.</td></tr>'
    summary = ""
    if errors:
        summary = '<div class="notice error" role="alert"><strong>That limit was not saved.</strong> Correct the form below.</div>'
    fields = "".join(
        [
            _field(
                "sku",
                "SKU",
                values.get("sku", ""),
                errors,
                help_text="The exact SKU as it appears on the listing.",
            ),
            _field(
                "floor_price",
                "Floor price",
                values.get("floor_price", ""),
                errors,
                help_text=(
                    "An automated price change can never take this SKU below this amount. "
                    "Setting a floor is required before this SKU can be changed automatically."
                ),
            ),
            _field(
                "max_change_percent",
                "Change-cap override (optional)",
                values.get("max_change_percent", ""),
                errors,
                required=False,
                help_text=(
                    f"Leave blank to use the global cap (currently {h(default_change_percent)}% per change). "
                    "Set a different number here only for this one SKU -- for example a lower cap for "
                    "a high-value item, or a higher one for fast-moving stock."
                ),
            ),
        ]
    )
    body = f"""{summary}<div class="notice info"><strong>These limits apply to every future automated
    price change.</strong> No eBay data is changed by this page -- it only stores the limits this
    application's own Section 6 safety checks enforce before any automated change is allowed.</div>
    <div class="table-wrap"><table><thead><tr><th>SKU</th><th>Floor price</th>
      <th>Change cap</th><th>Last updated</th><th></th>
    </tr></thead><tbody>{rows}</tbody></table></div>
    <form method="post" action="/price-limits" class="card">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}">{fields}
      <button type="submit">Save limit</button>
    </form>"""
    return layout(
        "Price safety limits",
        body,
        authenticated=True,
        csrf_token=csrf_token,
        flashes=flashes,
    )


def price_review_page(token: str, payload: Mapping[str, Any], csrf_token: str) -> str:
    body = f"""<div class="notice warning"><strong>No price change has been made yet.</strong>
    The current Offer will be checked again immediately before confirmation.</div>
    <section class="card"><dl><dt>SKU</dt><dd><code>{h(payload['sku'])}</code></dd>
      <dt>Offer</dt><dd><code>{h(payload['offer_id'])}</code></dd>
      <dt>Current price</dt><dd>{h(payload['currency'])} {h(payload['expected_price'])}</dd>
      <dt>New price</dt><dd><strong>{h(payload['currency'])} {h(payload['new_price'])}</strong></dd></dl></section>
    <div class="actions"><form method="post" action="/actions/confirm">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}"><input type="hidden" name="action_token" value="{h(token)}">
      <button type="submit" class="danger">Confirm live price change</button></form>
    <form method="post" action="/actions/cancel"><input type="hidden" name="csrf_token" value="{h(csrf_token)}">
      <input type="hidden" name="action_token" value="{h(token)}"><button type="submit" class="secondary">Cancel</button></form></div>"""
    return layout("Review price change", body, authenticated=True, csrf_token=csrf_token)


def price_action_page(
    action: Mapping[str, Any], csrf_token: str, environment: str = "Sandbox"
) -> str:
    """Render only the small, allowlisted Phase 6 reconciliation summary."""

    status = str(action.get("status", "unknown"))
    messages = {
        "succeeded": ("success", f"The {environment} Offer price was changed and confirmed."),
        "failed": ("error", "The price change stopped before an eBay write was confirmed."),
        "unknown": (
            "warning",
            f"The result is uncertain. Check this Offer in eBay {environment} before doing "
            "anything else.",
        ),
        "cancelled": ("info", "The confirmation was cancelled before execution."),
        "expired": ("info", "The confirmation expired before execution."),
    }
    notice_class, message = messages.get(
        status,
        ("warning", "This price action requires review before another attempt."),
    )
    result = action.get("result")
    safe_result = result if isinstance(result, Mapping) else {}
    if safe_result.get("dry_run") is True:
        # Section 6 kill switch: this confirmation never reached eBay at all.
        # Overrides the ordinary "succeeded" wording unconditionally -- a
        # dry run must never be mistaken for a real price change.
        notice_class, message = (
            "warning",
            "DRY RUN -- no eBay change was made. This is a rehearsal of what "
            "would have happened with APP_DRY_RUN=false.",
        )
    result_fields = (
        ("Dry run", "dry_run"),
        ("SKU", "sku"),
        ("Offer ID", "offer_id"),
        ("New price", "price"),
        ("Currency", "currency"),
        ("Safe error code", "safe_code"),
        ("Reconciliation required", "reconciliation_required"),
    )
    result_rows = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value)}</dd>"
        for label, key in result_fields
        if (value := safe_result.get(key)) is not None
    ) or "<dt>Result</dt><dd>No additional public details were stored.</dd>"
    guidance = ""
    if status == "unknown" or safe_result.get("reconciliation_required") is True:
        guidance = f"""<div class="notice warning"><strong>Do not retry this Offer yet.</strong>
        First inspect the Offer in eBay {h(environment)}. The application deliberately does not retry
        an uncertain write.</div>"""
    body = f"""<div class="notice {h(notice_class)}" role="status"><strong>Status:
    {h(status)}</strong> {h(message)}</div>{guidance}
    <section class="card"><h2>Safe result summary</h2><dl>{result_rows}</dl></section>
    <p><a class="button secondary" href="/">Back to dashboard</a></p>"""
    return layout(
        f"{environment} price change result",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def quantity_review_page(token: str, payload: Mapping[str, Any], csrf_token: str) -> str:
    body = f"""<div class="notice warning"><strong>No quantity change has been made yet.</strong>
    The current Offer will be checked again immediately before confirmation.</div>
    <section class="card"><dl><dt>SKU</dt><dd><code>{h(payload['sku'])}</code></dd>
      <dt>Offer</dt><dd><code>{h(payload['offer_id'])}</code></dd>
      <dt>Current quantity</dt><dd>{h(payload['expected_quantity'])}</dd>
      <dt>New quantity</dt><dd><strong>{h(payload['new_quantity'])}</strong></dd></dl></section>
    <div class="actions"><form method="post" action="/actions/confirm">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}"><input type="hidden" name="action_token" value="{h(token)}">
      <button type="submit" class="danger">Confirm live quantity change</button></form>
    <form method="post" action="/actions/cancel"><input type="hidden" name="csrf_token" value="{h(csrf_token)}">
      <input type="hidden" name="action_token" value="{h(token)}"><button type="submit" class="secondary">Cancel</button></form></div>"""
    return layout("Review quantity change", body, authenticated=True, csrf_token=csrf_token)


def quantity_action_page(
    action: Mapping[str, Any], csrf_token: str, environment: str = "Sandbox"
) -> str:
    """Render only the small, allowlisted Phase 7 reconciliation summary."""

    status = str(action.get("status", "unknown"))
    messages = {
        "succeeded": ("success", f"The {environment} Offer quantity was changed and confirmed."),
        "failed": ("error", "The quantity change stopped before an eBay write was confirmed."),
        "unknown": (
            "warning",
            f"The result is uncertain. Check this Offer in eBay {environment} before doing "
            "anything else.",
        ),
        "cancelled": ("info", "The confirmation was cancelled before execution."),
        "expired": ("info", "The confirmation expired before execution."),
    }
    notice_class, message = messages.get(
        status,
        ("warning", "This quantity action requires review before another attempt."),
    )
    result = action.get("result")
    safe_result = result if isinstance(result, Mapping) else {}
    if safe_result.get("dry_run") is True:
        # Section 6 kill switch: this confirmation never reached eBay at all.
        # Overrides the ordinary "succeeded" wording unconditionally -- a
        # dry run must never be mistaken for a real quantity change.
        notice_class, message = (
            "warning",
            "DRY RUN -- no eBay change was made. This is a rehearsal of what "
            "would have happened with APP_DRY_RUN=false.",
        )
    result_fields = (
        ("Dry run", "dry_run"),
        ("SKU", "sku"),
        ("Offer ID", "offer_id"),
        ("New quantity", "quantity"),
        ("Safe error code", "safe_code"),
        ("Reconciliation required", "reconciliation_required"),
    )
    result_rows = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value)}</dd>"
        for label, key in result_fields
        if (value := safe_result.get(key)) is not None
    ) or "<dt>Result</dt><dd>No additional public details were stored.</dd>"
    guidance = ""
    if status == "unknown" or safe_result.get("reconciliation_required") is True:
        guidance = f"""<div class="notice warning"><strong>Do not retry this Offer yet.</strong>
        First inspect the Offer in eBay {h(environment)}. The application deliberately does not retry
        an uncertain write.</div>"""
    body = f"""<div class="notice {h(notice_class)}" role="status"><strong>Status:
    {h(status)}</strong> {h(message)}</div>{guidance}
    <section class="card"><h2>Safe result summary</h2><dl>{result_rows}</dl></section>
    <p><a class="button secondary" href="/">Back to dashboard</a></p>"""
    return layout(
        f"{environment} quantity change result",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def image_review_page(token: str, payload: Mapping[str, Any], csrf_token: str) -> str:
    body = f"""<div class="notice warning"><strong>No image has been added yet.</strong>
    The current listing images will be checked again immediately before confirmation.</div>
    <section class="card"><dl><dt>SKU</dt><dd><code>{h(payload['sku'])}</code></dd>
      <dt>Offer</dt><dd><code>{h(payload['offer_id'])}</code></dd>
      <dt>New image</dt><dd><strong>{h(payload['new_image_url'])}</strong></dd>
      <dt>Content type</dt><dd>{h(payload['content_type'])}</dd>
      <dt>Size (bytes)</dt><dd>{h(payload['content_length'])}</dd></dl></section>
    <div class="actions"><form method="post" action="/actions/confirm">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}"><input type="hidden" name="action_token" value="{h(token)}">
      <button type="submit" class="danger">Confirm live image addition</button></form>
    <form method="post" action="/actions/cancel"><input type="hidden" name="csrf_token" value="{h(csrf_token)}">
      <input type="hidden" name="action_token" value="{h(token)}"><button type="submit" class="secondary">Cancel</button></form></div>"""
    return layout("Review image addition", body, authenticated=True, csrf_token=csrf_token)


def image_action_page(
    action: Mapping[str, Any], csrf_token: str, environment: str = "Sandbox"
) -> str:
    """Render only the small, allowlisted Phase 8 reconciliation summary."""

    status = str(action.get("status", "unknown"))
    messages = {
        "succeeded": ("success", f"The {environment} listing image was added and confirmed."),
        "failed": ("error", "The image addition stopped before an eBay write was confirmed."),
        "unknown": (
            "warning",
            f"The result is uncertain. Check this Offer in eBay {environment} before doing "
            "anything else.",
        ),
        "cancelled": ("info", "The confirmation was cancelled before execution."),
        "expired": ("info", "The confirmation expired before execution."),
    }
    notice_class, message = messages.get(
        status,
        ("warning", "This image action requires review before another attempt."),
    )
    result = action.get("result")
    safe_result = result if isinstance(result, Mapping) else {}
    if safe_result.get("dry_run") is True:
        # Section 6 kill switch: this confirmation never reached eBay at all.
        # Overrides the ordinary "succeeded" wording unconditionally -- a
        # dry run must never be mistaken for a real image addition.
        notice_class, message = (
            "warning",
            "DRY RUN -- no eBay change was made. This is a rehearsal of what "
            "would have happened with APP_DRY_RUN=false.",
        )
    result_fields = (
        ("Dry run", "dry_run"),
        ("SKU", "sku"),
        ("Offer ID", "offer_id"),
        ("New image", "image_url"),
        ("Image count", "image_count"),
        ("Safe error code", "safe_code"),
        ("Reconciliation required", "reconciliation_required"),
    )
    result_rows = "".join(
        f"<dt>{h(label)}</dt><dd>{h(value)}</dd>"
        for label, key in result_fields
        if (value := safe_result.get(key)) is not None
    ) or "<dt>Result</dt><dd>No additional public details were stored.</dd>"
    guidance = ""
    if status == "unknown" or safe_result.get("reconciliation_required") is True:
        guidance = f"""<div class="notice warning"><strong>Do not retry this Offer yet.</strong>
        First inspect the Offer in eBay {h(environment)}. The application deliberately does not retry
        an uncertain write.</div>"""
    body = f"""<div class="notice {h(notice_class)}" role="status"><strong>Status:
    {h(status)}</strong> {h(message)}</div>{guidance}
    <section class="card"><h2>Safe result summary</h2><dl>{result_rows}</dl></section>
    <p><a class="button secondary" href="/">Back to dashboard</a></p>"""
    return layout(
        f"{environment} image addition result",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def action_page(
    action: Mapping[str, Any], csrf_token: str, environment: str = "Sandbox"
) -> str:
    result = action.get("result")
    result_html = (
        f"<h2>Result</h2><pre>{h(json.dumps(result, indent=2, sort_keys=True))}</pre>"
        if result is not None
        else ""
    )
    error_html = (
        f'<div class="notice error" role="alert">{h(action.get("error_message"))}</div>'
        if action.get("error_message")
        else ""
    )
    guidance = ""
    if action.get("status") == "unknown":
        guidance = (
            '<div class="notice warning"><strong>Reconcile before retrying.</strong>'
            f'Check the SKU or Offer in eBay {h(environment)} first because the application '
            "cannot determine whether the confirmed change completed.</div>"
        )
    elif action.get("status") == "partial":
        guidance = (
            '<div class="notice warning"><strong>Partial draft.</strong>'
            "The Inventory item exists, but the unpublished Offer was not confirmed. "
            f"Check eBay {h(environment)} before taking another action.</div>"
        )
    body = f"""<p>Status: <span class="badge {h(action['status'])}">{h(action['status'])}</span></p>
    {guidance}{error_html}{result_html}<p><a class="button secondary" href="/">Back to dashboard</a></p>"""
    return layout("Confirmed action", body, authenticated=True, csrf_token=csrf_token)


def error_page(title: str, message: str, csrf_token: str, authenticated: bool) -> str:
    return layout(
        title,
        f'<div class="notice error" role="alert">{h(message)}</div><p><a href="/">Return to dashboard</a></p>',
        authenticated=authenticated,
        csrf_token=csrf_token,
    )


def account_deletion_banner_page(deletion_state: Mapping[str, Any], csrf_token: str) -> str:
    """Blocking page shown whenever eBay has reported this application's own
    connected account closed and the owner has not yet confirmed it (see
    ebay_app/account_deletion.py and WebApplication's gating in web.py).
    """
    detected_at = deletion_state.get("detected_at")
    detected = ""
    if isinstance(detected_at, int):
        detected = datetime.fromtimestamp(detected_at, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    body = f"""<div class="notice error" role="alert"><strong>eBay reports this connected account was closed.</strong>
      eBay's Marketplace Account Deletion/Closure notification was received and its signature verified{f' at {h(detected)}' if detected else ''}.
      This application must stop using its stored access for that account before doing anything else.</div>
    <section class="card">
      <h2>What happens if you confirm</h2>
      <ul>
        <li>The application's locally stored eBay connection (its refresh token) is deleted.</li>
        <li>Any pending, unconfirmed action is cancelled.</li>
        <li>Nothing is changed on eBay itself -- only this application's local copy of the connection.</li>
      </ul>
      <p>If you believe this is a mistake (for example, a different account than the one you expect), you can
      still check the eBay Developer Portal yourself before confirming.</p>
      <form method="post" action="/account-deletion">
        <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
        <button type="submit">Confirm and disconnect</button>
      </form>
    </section>"""
    return layout(
        "Account closure reported by eBay",
        body,
        authenticated=True,
        csrf_token=csrf_token,
    )


def engine_page(
    status: Mapping[str, Any],
    pending: Sequence[Mapping[str, Any]],
    csrf_token: str,
    environment: str = "Sandbox",
    flashes: Sequence[Tuple[str, str]] = (),
) -> str:
    """Read-only engine report plus pause/resume/run-now controls."""
    from .engine import describe

    headline, detail = describe(dict(status))
    paused = bool(status.get("paused_reason"))
    kind = "warning" if paused or status.get("mode") == "off" else "info"
    control_action, control_label = ("/engine/resume", "Resume engine") if paused else ("/engine/pause", "Pause engine")
    controls = f"""<form method="post" action="{control_action}" class="inline">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
      <button type="submit">{h(control_label)}</button></form>
    <form method="post" action="/engine/run" class="inline">
      <input type="hidden" name="csrf_token" value="{h(csrf_token)}">
      <button type="submit" class="secondary">Re-check prices on next tick</button></form>"""
    margin = status.get("default_margin_percent")
    settings = (f"<p>Mode <code>{h(status.get('mode'))}</code> · reprice every "
                f"{h(status.get('reprice_hours'))} h · default margin "
                f"{h(str(margin) + '%' if margin else 'none (per-SKU only)')}</p>")
    report = status.get("report") or {}
    rows = report.get("rows") or []
    row_html = "".join(
        "<tr>"
        f"<td><code>{h(r.get('sku'))}</code></td>"
        f"<td>{h(r.get('currency'))} {h(r.get('live_price'))}</td>"
        f"<td>{h(r.get('total_cost') or '-')}</td>"
        f"<td>{h((r.get('implied_margin') + '%') if r.get('implied_margin') else '-')}</td>"
        f"<td>{h((r.get('margin_used') + '%') if r.get('margin_used') else '-')}</td>"
        f"<td>{h(r.get('target') or '-')}</td>"
        f"<td>{h(r.get('proposed') or '-')}</td>"
        f"<td>{h(r.get('status'))}</td>"
        f"<td>{h(r.get('reason'))}</td>"
        "</tr>"
        for r in rows
    ) or '<tr><td colspan="9">No report yet. The first run happens on the next engine tick.</td></tr>'
    generated = report.get("generated_at")
    def _proposal_buttons(p):
        pid = h(str(p["proposal_id"]))
        return (
            f'<form method="post" action="/engine/approve" class="inline">'
            f'<input type="hidden" name="csrf_token" value="{h(csrf_token)}">'
            f'<input type="hidden" name="proposal_id" value="{pid}">'
            f'<button type="submit">Approve</button></form> '
            f'<form method="post" action="/engine/reject" class="inline">'
            f'<input type="hidden" name="csrf_token" value="{h(csrf_token)}">'
            f'<input type="hidden" name="proposal_id" value="{pid}">'
            f'<button type="submit" class="secondary">Reject</button></form>'
        )

    executes = bool(status.get("executes"))
    pending_html = "".join(
        f"<tr><td><code>{h(p['sku'])}</code></td><td>{h(p['kind'])}</td>"
        f"<td>{h(p.get('currency') or '')} {h(p['current_value'])} &rarr; {h(p['proposed_value'])}</td>"
        f"<td>{h(p['decision'])}</td><td>{h(p['reason'])}"
        f"{('<br><em>' + h(p['outcome']) + '</em>') if p.get('outcome') else ''}</td>"
        f"<td>{_proposal_buttons(p) if executes else ''}</td></tr>"
        for p in pending
    ) or '<tr><td colspan="6">No pending proposals.</td></tr>'
    body = f"""<div class="notice {kind}"><strong>{h(headline)}</strong> {h(detail)}</div>
    <section class="card">{settings}{controls}</section>
    <section class="card"><h2>Last price check</h2>
    <p>{h('Generated at ' + str(generated) + ' (Unix time) · ' + str(report.get('listings', 0)) + ' listings · ' + str(report.get('proposals', 0)) + ' proposals') if generated else 'Not run yet.'}</p>
    <div class="table-wrap"><table><thead><tr><th>SKU</th><th>Live</th><th>Cost</th>
    <th>Current margin</th><th>Target margin</th><th>Target</th><th>Proposed</th><th>Status</th><th>Why</th></tr></thead>
    <tbody>{row_html}</tbody></table></div></section>
    <section class="card"><h2>Pending proposals</h2>
    <p>{h('Approve runs the change through the normal price-change checks (floor, max change, actions budget, live-price re-check, dry-run switch). Proposals expire after 24 hours.' if executes else 'Execution is unavailable in this configuration.')}</p>
    <div class="table-wrap"><table><thead><tr><th>SKU</th><th>Kind</th><th>Change</th><th>Route</th><th>Why</th><th></th></tr></thead>
    <tbody>{pending_html}</tbody></table></div></section>"""
    return layout(f"Autonomy engine ({environment})", body, authenticated=True, csrf_token=csrf_token, flashes=flashes)
