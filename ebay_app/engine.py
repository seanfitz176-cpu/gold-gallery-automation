"""Autonomy engine core (docs/autonomy_engine.md).

Runs as ONE background thread inside the existing app process, because
``AppDatabase`` holds an exclusive process lock. Jobs only PROPOSE changes.
A proposal is executed only (a) when the owner approves it on /engine, or
(b) automatically in ``auto-dry``/``auto`` mode when it was routed ``auto``
(inside every limit). Execution never talks to eBay directly: it goes
through ``PriceService.prepare_review`` -> ``confirm`` -- the same journaled,
drift-checked, floor/cap/budget-limited, dry-run-aware path as a manual
change. ``auto-dry`` forces dry run regardless of APP_DRY_RUN.

Safety properties:
- ``off`` (the default) does nothing at all.
- A persistent pause (survives restarts) stops every job on the next tick.
- Three consecutive job errors pause the engine automatically.
- Every eBay read goes through the normal verified-connection services.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from dataclasses import replace

from .config import AppConfig
from .ebay_client import EbayError
from .engine_pricing import SkuCost, decide_reprice
from .storage import AppDatabase
from .validation import ValidationError

ENGINE_SESSION_ID = "autonomy-engine"

PROPOSAL_TTL_SECONDS = 24 * 3600
MAX_REPORT_ROWS = 2000
AUTO_PAUSE_AFTER_ERRORS = 3

STATE_PAUSED = "paused_reason"
STATE_LAST_REPRICE = "last_reprice_at"
STATE_REPORT = "last_reprice_report"
STATE_ERRORS = "consecutive_errors"
STATE_REPRICE_REQUESTED = "reprice_requested"
STATE_LAST_RESTOCK = "last_restock_at"
STATE_LAST_DIGEST = "last_digest_at"


@dataclass(frozen=True)
class LiveListing:
    sku: str
    price_value: str
    currency: str
    available_quantity: Optional[int] = None


ListingReader = Callable[[], Iterable[LiveListing]]


class Engine:
    def __init__(
        self,
        config: AppConfig,
        database: AppDatabase,
        listing_reader: ListingReader,
        *,
        now: Callable[[], float] = time.time,
        logger: Optional[logging.Logger] = None,
        ebay=None,
        price_service=None,
        quantity_service=None,
        notifier=None,
    ) -> None:
        self.config = config
        self.database = database
        self.listing_reader = listing_reader
        self.now = now
        self.logger = logger or logging.getLogger("ebay_engine")
        self.ebay = ebay
        # The app's own PriceService, shared so there is ONE actions-per-run
        # budget for manual and engine changes together.
        self.price_service = price_service
        self.quantity_service = quantity_service
        # ebay_app.alerts.Notifier; None or unconfigured = no emails.
        self.notifier = notifier
        self._dry_run_services: Dict[str, object] = {}
        self._execute_lock = threading.Lock()

    # -- pause / status ---------------------------------------------------

    def pause(self, reason: str) -> None:
        already = self.paused_reason()
        self.database.set_engine_state(STATE_PAUSED, reason[:300] or "Paused")
        self.logger.warning("Engine paused: %s", reason)
        if not already and self.notifier is not None:
            self.notifier.send(
                "Engine PAUSED",
                f"The autonomy engine has paused.\n\nReason: {reason}\n\n"
                "No job will run until you press Resume on http://127.0.0.1:5000/engine.",
            )

    def resume(self) -> None:
        self.database.delete_engine_state(STATE_PAUSED)
        self.database.set_engine_state(STATE_ERRORS, "0")
        self.logger.info("Engine resumed")

    def paused_reason(self) -> Optional[str]:
        return self.database.get_engine_state(STATE_PAUSED)

    def request_reprice(self) -> None:
        self.database.set_engine_state(STATE_REPRICE_REQUESTED, "1")

    def executes(self) -> bool:
        """Whether proposals can be executed (approved or auto)."""
        return self.ebay is not None and (self.price_service is not None or self.quantity_service is not None)

    def status(self) -> Dict[str, object]:
        report_raw = self.database.get_engine_state(STATE_REPORT)
        try:
            report = json.loads(report_raw) if report_raw else None
        except ValueError:
            report = None
        last = self.database.get_engine_state(STATE_LAST_REPRICE)
        return {
            "mode": self.config.engine_mode,
            "paused_reason": self.paused_reason(),
            "executes": self.executes(),
            "last_reprice_at": int(last) if last and last.isdigit() else None,
            "reprice_hours": self.config.engine_reprice_hours,
            "default_margin_percent": (
                str(self.config.engine_default_margin_percent)
                if self.config.engine_default_margin_percent is not None else None
            ),
            "report": report,
        }

    # -- scheduling -------------------------------------------------------

    def _reprice_due(self) -> bool:
        if self.database.get_engine_state(STATE_REPRICE_REQUESTED) == "1":
            return True
        last = self.database.get_engine_state(STATE_LAST_REPRICE)
        if not last or not last.isdigit():
            return True
        return self.now() - int(last) >= self.config.engine_reprice_hours * 3600

    def _restock_due(self) -> bool:
        last = self.database.get_engine_state(STATE_LAST_RESTOCK)
        if not last or not last.isdigit():
            return True
        return self.now() - int(last) >= self.config.engine_relist_minutes * 60

    def tick(self) -> None:
        """One scheduler pass. Safe to call at any time."""
        if self.config.engine_mode == "off":
            return
        self.database.expire_engine_proposals()
        if self.paused_reason():
            return
        try:
            if self._reprice_due():
                self.run_reprice()
            if self._restock_due():
                self.run_restock()
            self.run_auto()
            self.maybe_send_digest()
        except Exception as error:  # noqa: BLE001 - recorded, counted, never re-raised
            count = int(self.database.get_engine_state(STATE_ERRORS) or "0") + 1
            self.database.set_engine_state(STATE_ERRORS, str(count))
            self.logger.warning("Engine job failed (%s in a row): %s", count, type(error).__name__)
            if count >= AUTO_PAUSE_AFTER_ERRORS:
                self.pause(f"Paused automatically after {count} consecutive job errors "
                           f"(last: {type(error).__name__}: {error})")
            return
        self.database.set_engine_state(STATE_ERRORS, "0")

    # -- execution ------------------------------------------------------------

    def _service(self, kind: str, force_dry_run: bool):
        shared = self.price_service if kind == "price" else self.quantity_service
        if not force_dry_run or shared is None:
            return shared
        if kind not in self._dry_run_services:
            from .prices import PriceService
            from .quantities import QuantityService

            cls = PriceService if kind == "price" else QuantityService
            self._dry_run_services[kind] = cls(self.ebay, self.database, replace(self.config, dry_run=True))
        return self._dry_run_services[kind]

    def _finish(self, proposal_id: int, status: str, outcome: str, action_id: Optional[str] = None) -> str:
        self.database.set_engine_proposal_status(
            proposal_id, status, expected="approved", outcome=outcome, action_id=action_id
        )
        if status == "unknown":
            self.pause(f"Paused automatically: the outcome of proposal {proposal_id} is unknown "
                       "-- check the listing in Seller Hub before resuming")
        return status

    def execute_proposal(self, proposal_id: int, *, force_dry_run: bool = False) -> str:
        """Execute one pending price proposal through PriceService.

        Returns the proposal's final status, or "not_pending" (nothing done),
        or "budget" (actions-per-run budget reached; proposal left pending).
        """
        if not self.executes():
            return "not_pending"
        with self._execute_lock:
            proposal = self.database.get_engine_proposal(proposal_id)
            if proposal is None or proposal["status"] != "pending":
                return "not_pending"
            if proposal["expires_at"] <= self.now():
                self.database.set_engine_proposal_status(proposal_id, "expired", outcome="Expired before execution.")
                return "expired"
            kind = proposal["kind"]
            if kind not in {"price", "quantity"}:
                return "not_pending"  # e.g. "republish" is advice only, never executed
            service = self._service(kind, force_dry_run)
            if service is None:
                return "not_pending"
            if not self.database.set_engine_proposal_status(proposal_id, "approved"):
                return "not_pending"
            sku = str(proposal["sku"])
            confirm_started = False
            try:
                offers = self.ebay.list_offers(sku)
                published = [o for o in offers if o.get("status") == "PUBLISHED"]
                if len(published) != 1 or not isinstance(published[0].get("offerId"), str):
                    return self._finish(proposal_id, "failed",
                                        f"Expected exactly one published Offer for this SKU, found {len(published)}.")
                offer_id = published[0]["offerId"]
                field = "new_price" if kind == "price" else "new_quantity"
                review = service.prepare_review(
                    {"offer_id": [offer_id], "sku": [sku], field: [str(proposal["proposed_value"])]},
                    ENGINE_SESSION_ID,
                )
                if kind == "price":
                    live, expected = Decimal(review.expected_price), Decimal(str(proposal["current_value"]))
                else:
                    live, expected = int(review.expected_quantity), int(str(proposal["current_value"]))
                if live != expected:
                    service.cancel(review.action_token, ENGINE_SESSION_ID)
                    return self._finish(proposal_id, "superseded",
                                        f"Live {kind} is now {live}, not {proposal['current_value']}; "
                                        "nothing changed.")
                confirm_started = True
                action_id = service.confirm(review.action_token, ENGINE_SESSION_ID)
                if action_id is None:
                    return self._finish(proposal_id, "failed", "The confirmation could not be claimed; nothing changed.")
                action = self.database.get_action_for_admin(action_id) or {}
                result = action.get("result") or {}
                state = action.get("status")
                if state == "succeeded":
                    note = ("DRY RUN -- no eBay change was made." if result.get("dry_run")
                            else f"{kind.capitalize()} changed on eBay.")
                    return self._finish(proposal_id, "executed", note, action_id)
                if state == "failed":
                    return self._finish(proposal_id, "failed",
                                        f"eBay rejected the change: {action.get('error_message') or 'see audit log'}",
                                        action_id)
                return self._finish(proposal_id, "unknown",
                                    "The outcome could not be confirmed -- check the listing in Seller Hub.", action_id)
            except EbayError as error:
                if error.code == "MAX_ACTIONS_PER_RUN_REACHED" and not confirm_started:
                    self.database.set_engine_proposal_status(
                        proposal_id, "pending", expected="approved",
                        outcome="Waiting: this run's actions budget is used up (restart the app to reset).")
                    return "budget"
                if confirm_started and error.outcome_unknown:
                    return self._finish(proposal_id, "unknown", f"{error.code}: {error}")
                return self._finish(proposal_id, "failed", f"{error.code}: {error}")
            except ValidationError as error:
                return self._finish(proposal_id, "failed", "; ".join(error.errors.values()))
            except Exception as error:  # noqa: BLE001
                if confirm_started:
                    return self._finish(proposal_id, "unknown", f"Unexpected error after confirming: {type(error).__name__}")
                return self._finish(proposal_id, "failed", f"Unexpected error before any change: {type(error).__name__}")

    def run_auto(self) -> int:
        """Execute pending ``auto`` proposals in auto-dry/auto mode only."""
        if self.config.engine_mode not in {"auto-dry", "auto"} or not self.executes():
            return 0
        executed = 0
        pending = sorted(
            (p for p in self.database.list_engine_proposals(("pending",)) if p["decision"] == "auto"),
            key=lambda p: (p["created_at"], p["proposal_id"]),
        )
        for proposal in pending:
            if self.paused_reason():
                break
            outcome = self.execute_proposal(proposal["proposal_id"],
                                            force_dry_run=self.config.engine_mode == "auto-dry")
            if outcome == "budget":
                break
            if outcome in {"executed", "failed", "unknown", "superseded"}:
                executed += 1
        return executed

    # -- digest email ---------------------------------------------------------

    def maybe_send_digest(self) -> bool:
        """At most every APP_ENGINE_DIGEST_HOURS, and only if something
        happened or is waiting for the owner."""
        if self.notifier is None or not getattr(self.notifier, "configured", False):
            return False
        now = int(self.now())
        last_raw = self.database.get_engine_state(STATE_LAST_DIGEST)
        last = int(last_raw) if last_raw and last_raw.isdigit() else 0
        if last and now - last < self.config.engine_digest_hours * 3600:
            return False
        pending = self.database.list_engine_proposals(("pending",), limit=200)
        waiting = [p for p in pending if p["decision"] == "queue"]
        finished = [p for p in self.database.list_engine_proposals(
            ("executed", "failed", "unknown", "superseded"), limit=500) if p["updated_at"] > last]
        report = (self.status().get("report") or {})
        no_cost = sum(1 for r in report.get("rows", []) if r.get("status") == "no_cost")
        if not waiting and not finished:
            self.database.set_engine_state(STATE_LAST_DIGEST, str(now))
            return False
        lines = [f"Mode: {self.config.engine_mode}", ""]
        if waiting:
            lines.append(f"{len(waiting)} proposal(s) waiting for your approval:")
            lines += [f"  {p['sku']}: {p['kind']} {p['current_value']} -> {p['proposed_value']}"
                      for p in waiting[:20]]
            lines.append("")
        if finished:
            lines.append("Since the last email:")
            lines += [f"  {p['sku']}: {p['kind']} {p['current_value']} -> {p['proposed_value']} "
                      f"[{p['status']}] {p.get('outcome') or ''}" for p in finished[:40]]
            lines.append("")
        if no_cost:
            lines.append(f"{no_cost} live listing(s) have no cost recorded and are never repriced.")
        lines.append("Review: http://127.0.0.1:5000/engine")
        sent = self.notifier.send(f"Engine digest: {len(waiting)} waiting, {len(finished)} done", "\n".join(lines))
        if sent:
            self.database.set_engine_state(STATE_LAST_DIGEST, str(now))
        return sent

    # -- restock job --------------------------------------------------------

    def run_restock(self) -> int:
        """Turn each pending one-off restock instruction into ONE proposal.

        The instruction is cleared as soon as its proposal exists (or when
        nothing needs changing), so it is never re-applied: the engine cannot
        see later sales, and re-applying an old stock figure would oversell.
        """
        pending = [SkuCost.from_row(r) for r in self.database.list_sku_costs() if r.get("stock_pending")]
        if not pending:
            self.database.set_engine_state(STATE_LAST_RESTOCK, str(int(self.now())))
            return 0
        live = {l.sku: l for l in self.listing_reader()}
        auto_mode = self.config.engine_mode in {"auto-dry", "auto"}
        created = 0
        for cost in pending:
            stock = cost.stock_quantity
            listing = live.get(cost.sku)
            if stock is None:
                self.database.clear_stock_pending(cost.sku)
                continue
            if listing is None:
                if stock > 0:
                    self.database.record_engine_proposal(
                        sku=cost.sku, kind="republish", current_value="not live",
                        proposed_value=f"publish with quantity {stock}", currency=None,
                        reason=(f"Restock instruction for {stock}, but this SKU has no live listing. "
                                "Publish it with bulk_publish_listings.py; the engine never "
                                "republishes by itself."),
                        decision="queue", ttl_seconds=PROPOSAL_TTL_SECONDS,
                    )
                    created += 1
            elif listing.available_quantity is None or listing.available_quantity != stock:
                current = listing.available_quantity if listing.available_quantity is not None else 0
                action = "Relist" if current == 0 and stock > 0 else "Set stock"
                self.database.record_engine_proposal(
                    sku=cost.sku, kind="quantity", current_value=str(current),
                    proposed_value=str(stock), currency=None,
                    reason=f"{action}: restock instruction says {stock}; listing shows {current}.",
                    decision="auto" if auto_mode else "queue", ttl_seconds=PROPOSAL_TTL_SECONDS,
                )
                created += 1
            self.database.clear_stock_pending(cost.sku)
        self.database.set_engine_state(STATE_LAST_RESTOCK, str(int(self.now())))
        self.logger.info("Engine restock run: %s instruction(s), %s proposal(s)", len(pending), created)
        return created

    # -- reprice job --------------------------------------------------------

    def run_reprice(self) -> List[Dict[str, object]]:
        listings = list(self.listing_reader())
        costs = {row["sku"]: SkuCost.from_row(row) for row in self.database.list_sku_costs()}
        auto_mode = self.config.engine_mode in {"auto-dry", "auto"}
        rows: List[Dict[str, object]] = []
        proposals = 0
        for listing in listings:
            row: Dict[str, object] = {"sku": listing.sku, "live_price": listing.price_value,
                                      "currency": listing.currency}
            cost = costs.get(listing.sku)
            try:
                live = Decimal(listing.price_value)
            except (InvalidOperation, TypeError):
                rows.append({**row, "status": "skipped", "reason": "Live price unreadable."})
                continue
            if cost is None:
                rows.append({**row, "status": "no_cost",
                             "reason": "No cost recorded; add it with import_costs.py. Not repriced."})
                continue
            if cost.currency != listing.currency:
                rows.append({**row, "status": "skipped",
                             "reason": f"Cost currency {cost.currency} differs from listing currency."})
                continue
            limit = self.database.get_price_limit(listing.sku)
            floor = Decimal(limit["floor_price"]) if limit else None
            max_change = (
                Decimal(limit["max_change_percent"])
                if limit and limit.get("max_change_percent")
                else self.config.max_price_change_percent
            )
            decision = decide_reprice(
                cost, live,
                default_margin_percent=self.config.engine_default_margin_percent,
                max_change_percent=max_change,
                floor_price=floor,
                require_floor=self.config.require_price_floor,
            )
            row.update({
                "total_cost": str(cost.total_cost),
                "implied_margin": str(decision.implied_margin),
                "margin_used": str(decision.margin_used) if decision.margin_used is not None else None,
                "target": str(decision.target) if decision.target is not None else None,
                "proposed": str(decision.proposed) if decision.proposed is not None else None,
                "reason": decision.reason,
            })
            if decision.proposed is None:
                row["status"] = "no_margin" if decision.margin_used is None else "on_target"
            else:
                route = "auto" if (auto_mode and decision.within_limits) else "queue"
                self.database.record_engine_proposal(
                    sku=listing.sku, kind="price", current_value=str(live),
                    proposed_value=str(decision.proposed), currency=listing.currency,
                    reason=decision.reason, decision=route, ttl_seconds=PROPOSAL_TTL_SECONDS,
                )
                proposals += 1
                row["status"] = f"proposed_{route}"
            rows.append(row)
        report = {
            "generated_at": int(self.now()),
            "mode": self.config.engine_mode,
            "listings": len(listings),
            "proposals": proposals,
            "rows": rows[:MAX_REPORT_ROWS],
            "truncated": len(rows) > MAX_REPORT_ROWS,
        }
        self.database.set_engine_state(STATE_REPORT, json.dumps(report, separators=(",", ":")))
        self.database.set_engine_state(STATE_LAST_REPRICE, str(int(self.now())))
        self.database.delete_engine_state(STATE_REPRICE_REQUESTED)
        self.logger.info("Engine reprice run: %s listings, %s proposals (mode=%s)",
                         len(listings), proposals, self.config.engine_mode)
        return rows


class EngineThread:
    """Calls ``engine.tick()`` every ``interval_seconds`` until stopped."""

    def __init__(self, engine: Engine, interval_seconds: float = 60.0) -> None:
        self.engine = engine
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.engine.config.engine_mode == "off" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ebay-engine", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.engine.tick()
            except Exception:  # noqa: BLE001 - the thread must never die silently
                self.engine.logger.exception("Unexpected engine error")
            self._stop.wait(self.interval_seconds)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


def listing_reader_from_service(listing_service) -> ListingReader:
    """Adapt ``ListingService.retrieve()`` (verified, scope-checked reads)."""

    def read() -> Iterable[LiveListing]:
        summary = listing_service.retrieve()
        return [LiveListing(l.sku, l.price_value, l.currency, l.available_quantity) for l in summary.listings]

    return read


def describe(status: Dict[str, object]) -> Tuple[str, str]:
    """(headline, detail) for the dashboard and startup log."""
    mode = status["mode"]
    if mode == "off":
        return "Engine off", "Set APP_ENGINE_MODE=shadow to see proposals without any change being made."
    if status.get("paused_reason"):
        return "Engine PAUSED", str(status["paused_reason"])
    if not status.get("executes"):
        return (f"Engine running ({mode}) -- proposals only",
                "Execution is unavailable in this configuration.")
    if mode == "shadow":
        return ("Engine running (shadow)",
                "Proposals are only queued; nothing runs unless you approve it.")
    if mode == "auto-dry":
        return ("Engine running (auto-dry)",
                "In-limit proposals run automatically as DRY RUNS -- no eBay change is made.")
    return ("Engine running (AUTO)",
            "In-limit proposals change real prices, bound by APP_DRY_RUN and the actions budget.")
