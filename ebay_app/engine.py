"""Autonomy engine core (docs/autonomy_engine.md).

Runs as ONE background thread inside the existing app process, because
``AppDatabase`` holds an exclusive process lock. Jobs only PROPOSE changes;
nothing in this module writes to eBay. Execution of approved/auto proposals
through the existing, already-gated services is a later step -- until it
exists, every mode behaves as ``shadow`` for execution purposes, and the
status says so plainly.

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

from .config import AppConfig
from .engine_pricing import SkuCost, decide_reprice
from .storage import AppDatabase

PROPOSAL_TTL_SECONDS = 24 * 3600
MAX_REPORT_ROWS = 2000
AUTO_PAUSE_AFTER_ERRORS = 3

STATE_PAUSED = "paused_reason"
STATE_LAST_REPRICE = "last_reprice_at"
STATE_REPORT = "last_reprice_report"
STATE_ERRORS = "consecutive_errors"
STATE_REPRICE_REQUESTED = "reprice_requested"


@dataclass(frozen=True)
class LiveListing:
    sku: str
    price_value: str
    currency: str


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
    ) -> None:
        self.config = config
        self.database = database
        self.listing_reader = listing_reader
        self.now = now
        self.logger = logger or logging.getLogger("ebay_engine")

    # -- pause / status ---------------------------------------------------

    def pause(self, reason: str) -> None:
        self.database.set_engine_state(STATE_PAUSED, reason[:300] or "Paused")
        self.logger.warning("Engine paused: %s", reason)

    def resume(self) -> None:
        self.database.delete_engine_state(STATE_PAUSED)
        self.database.set_engine_state(STATE_ERRORS, "0")
        self.logger.info("Engine resumed")

    def paused_reason(self) -> Optional[str]:
        return self.database.get_engine_state(STATE_PAUSED)

    def request_reprice(self) -> None:
        self.database.set_engine_state(STATE_REPRICE_REQUESTED, "1")

    def executes(self) -> bool:
        """Whether this build can execute proposals. Execution through the
        existing services is not built yet, so this is always False."""
        return False

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

    def tick(self) -> None:
        """One scheduler pass. Safe to call at any time."""
        if self.config.engine_mode == "off":
            return
        self.database.expire_engine_proposals()
        if self.paused_reason():
            return
        if not self._reprice_due():
            return
        try:
            self.run_reprice()
        except Exception as error:  # noqa: BLE001 - recorded, counted, never re-raised
            count = int(self.database.get_engine_state(STATE_ERRORS) or "0") + 1
            self.database.set_engine_state(STATE_ERRORS, str(count))
            self.logger.warning("Engine reprice job failed (%s in a row): %s", count, type(error).__name__)
            if count >= AUTO_PAUSE_AFTER_ERRORS:
                self.pause(f"Paused automatically after {count} consecutive job errors "
                           f"(last: {type(error).__name__}: {error})")
            return
        self.database.set_engine_state(STATE_ERRORS, "0")

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
        return [LiveListing(l.sku, l.price_value, l.currency) for l in summary.listings]

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
                "Nothing is executed yet: approving or auto-running proposals is a later build step.")
    return f"Engine running ({mode})", ""
