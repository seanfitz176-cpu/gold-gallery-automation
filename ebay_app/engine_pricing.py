"""Pure pricing arithmetic for the autonomy engine (docs/autonomy_engine.md §4, §10).

No network, no database, no clock: every function takes plain values and
returns plain values, so every rule here is directly unit-tested. Money is
always ``Decimal`` and is rounded half-up to the penny, never ``float``.

The model, as chosen by the owner:
- cost = item cost + postage cost, plus eBay fees taken as a % of the sale
  price (0% for a UK private seller, see §10);
- target price solves  price × (1 − fee%) = cost × (1 + margin%);
- break-even price solves  price × (1 − fee%) = cost  -- a hard floor;
- a SKU with no margin (per-SKU or global) is never repriced; its implied
  current margin is reported instead.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

PENNY = Decimal("0.01")
HUNDRED = Decimal("100")
MAX_MONEY = Decimal("999999999.99")
MAX_MARGIN_PERCENT = Decimal("1000")
MAX_FEE_PERCENT = Decimal("50")
# A live price within this % of the target is left alone, so rounding and
# tiny differences never generate a stream of pointless one-penny changes.
DEFAULT_TOLERANCE_PERCENT = Decimal("1")


class CostRowError(ValueError):
    """One cost-table row failed validation."""


def _money(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise CostRowError(f"{field} must be a number like 12.50") from exc
    if not value.is_finite() or value < 0 or value > MAX_MONEY:
        raise CostRowError(f"{field} must be between 0 and {MAX_MONEY}")
    if value != value.quantize(PENNY):
        raise CostRowError(f"{field} can have at most 2 decimal places")
    return value.quantize(PENNY)


def _percent(raw: str, field: str, maximum: Decimal) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise CostRowError(f"{field} must be a number like 30") from exc
    if not value.is_finite() or value < 0 or value > maximum:
        raise CostRowError(f"{field} must be between 0 and {maximum}")
    if value != value.quantize(PENNY):
        raise CostRowError(f"{field} can have at most 2 decimal places")
    return value.quantize(PENNY)


@dataclass(frozen=True)
class SkuCost:
    sku: str
    item_cost: Decimal
    postage_cost: Decimal
    fee_percent: Decimal
    margin_percent: Optional[Decimal]
    currency: str
    stock_quantity: Optional[int] = None
    stock_pending: bool = False

    @property
    def total_cost(self) -> Decimal:
        return self.item_cost + self.postage_cost

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "SkuCost":
        margin = row.get("margin_percent")
        return cls(
            sku=str(row["sku"]),
            item_cost=Decimal(str(row["item_cost"])),
            postage_cost=Decimal(str(row["postage_cost"])),
            fee_percent=Decimal(str(row["fee_percent"])),
            margin_percent=Decimal(str(margin)) if margin not in (None, "") else None,
            currency=str(row["currency"]),
            stock_quantity=int(row["stock_quantity"]) if row.get("stock_quantity") is not None else None,
            stock_pending=bool(row.get("stock_pending")),
        )


def parse_cost_row(raw: Mapping[str, str], currency: str) -> SkuCost:
    """Validate one CSV row: sku, item_cost, postage_cost, and optional
    fee_percent (blank = 0) and margin_percent (blank = none)."""

    sku = (raw.get("sku") or "").strip()
    if not 1 <= len(sku) <= 50 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in sku):
        raise CostRowError("sku must be 1-50 printable characters")
    item_cost = _money(raw.get("item_cost") or "", "item_cost")
    postage_cost = _money(raw.get("postage_cost") or "0", "postage_cost")
    fee_raw = (raw.get("fee_percent") or "").strip()
    fee = _percent(fee_raw, "fee_percent", MAX_FEE_PERCENT) if fee_raw else Decimal("0.00")
    margin_raw = (raw.get("margin_percent") or "").strip()
    margin = _percent(margin_raw, "margin_percent", MAX_MARGIN_PERCENT) if margin_raw else None
    if item_cost + postage_cost <= 0:
        raise CostRowError("item_cost + postage_cost must be greater than 0")
    stock_raw = (raw.get("stock_quantity") or "").strip()
    stock: Optional[int] = None
    if stock_raw:
        if not stock_raw.isdigit() or len(stock_raw) > 6:
            raise CostRowError("stock_quantity must be a whole number from 0 to 999999")
        stock = int(stock_raw)
    return SkuCost(sku, item_cost, postage_cost, fee, margin, currency, stock, stock is not None)


def load_cost_csv(path: Path, currency: str) -> Tuple[List[SkuCost], List[Tuple[int, str, str]]]:
    """Parse a whole cost CSV. Returns (valid rows, [(line, sku, error)]).
    One bad row never blocks the others. No network call."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {(name or "").strip().lower() for name in (reader.fieldnames or [])}
        missing = {"sku", "item_cost"} - fields
        if missing:
            raise CostRowError("The CSV is missing required column(s): " + ", ".join(sorted(missing)))
        valid: List[SkuCost] = []
        errors: List[Tuple[int, str, str]] = []
        seen = set()
        for index, row in enumerate(reader):
            line = index + 2
            normalized: Dict[str, str] = {
                (k or "").strip().lower(): (v or "") for k, v in row.items() if k is not None
            }
            try:
                cost = parse_cost_row(normalized, currency)
            except CostRowError as error:
                errors.append((line, (normalized.get("sku") or "").strip(), str(error)))
                continue
            if cost.sku in seen:
                errors.append((line, cost.sku, "Duplicate SKU within this CSV file."))
                continue
            seen.add(cost.sku)
            valid.append(cost)
        return valid, errors


def _round(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


def break_even_price(cost: SkuCost) -> Decimal:
    """Lowest price that recovers item + postage after fees (rounded up)."""
    keep = 1 - cost.fee_percent / HUNDRED
    raw = cost.total_cost / keep
    rounded = _round(raw)
    return rounded if rounded >= raw else rounded + PENNY


def target_price(cost: SkuCost, margin_percent: Decimal) -> Decimal:
    keep = 1 - cost.fee_percent / HUNDRED
    return _round(cost.total_cost * (1 + margin_percent / HUNDRED) / keep)


def implied_margin_percent(cost: SkuCost, live_price: Decimal) -> Decimal:
    """The margin over cost the current live price actually delivers."""
    net = live_price * (1 - cost.fee_percent / HUNDRED)
    return _round((net - cost.total_cost) / cost.total_cost * HUNDRED)


@dataclass(frozen=True)
class MarketSummary:
    count: int
    low: Optional[Decimal]
    median: Optional[Decimal]
    high: Optional[Decimal]


MIN_COMPARABLES = 3
# A target more than this % above the median comparable price is never run
# automatically: it goes to the owner's queue with a note.
MARKET_ABOVE_MEDIAN_QUEUE_PERCENT = Decimal("25")


def summarize_market(prices) -> MarketSummary:
    values = sorted(Decimal(p) for p in prices)
    if not values:
        return MarketSummary(0, None, None, None)
    n = len(values)
    mid = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    return MarketSummary(n, values[0], _round(mid), values[-1])


def market_note(target: Optional[Decimal], market: MarketSummary) -> Tuple[Optional[str], bool]:
    """(note, must_queue). Informational unless the target is far above the
    market median, in which case the proposal must wait for the owner."""
    if market.count < MIN_COMPARABLES or market.median is None:
        return (f"Only {market.count} comparable listing(s) found; no market check." if market.count else None), False
    if target is None:
        return None, False
    limit = market.median * (1 + MARKET_ABOVE_MEDIAN_QUEUE_PERCENT / HUNDRED)
    if target > limit:
        return (f"Target {target} is more than {MARKET_ABOVE_MEDIAN_QUEUE_PERCENT}% above the market "
                f"median {market.median} ({market.count} comparables) -- needs your approval."), True
    if market.low is not None and target < market.low:
        return f"Target {target} is below the cheapest comparable ({market.low}).", False
    return None, False


@dataclass(frozen=True)
class RepriceDecision:
    sku: str
    live_price: Decimal
    implied_margin: Decimal
    margin_used: Optional[Decimal]
    target: Optional[Decimal]
    proposed: Optional[Decimal]  # None = no change proposed
    within_limits: bool  # False = must go to the owner's queue, never auto
    reason: str


def decide_reprice(
    cost: SkuCost,
    live_price: Decimal,
    *,
    default_margin_percent: Optional[Decimal],
    max_change_percent: Decimal,
    floor_price: Optional[Decimal],
    require_floor: bool,
    tolerance_percent: Decimal = DEFAULT_TOLERANCE_PERCENT,
) -> RepriceDecision:
    """Decide what (if anything) to propose for one SKU.

    Steps are clamped to ``max_change_percent`` so a large gap closes over
    several runs. The result never goes below the larger of break-even and
    the owner's floor. ``within_limits`` is False whenever execution would
    be refused by ``PriceService`` anyway (no floor while floors are
    required) or the proposal needs a human look (target below break-even
    or floor) -- such proposals are only ever queued.
    """

    implied = implied_margin_percent(cost, live_price)
    margin = cost.margin_percent if cost.margin_percent is not None else default_margin_percent
    if margin is None:
        return RepriceDecision(cost.sku, live_price, implied, None, None, None, False,
                               f"No margin set; current price gives {implied}% over cost. Not repriced.")

    target = target_price(cost, margin)
    minimum = break_even_price(cost)
    if floor_price is not None and floor_price > minimum:
        minimum = floor_price
    note = ""
    if target < minimum:
        note = f" Target {target} is below the minimum {minimum}; using the minimum."
        target = minimum

    if live_price > 0 and abs(target - live_price) / live_price * HUNDRED <= tolerance_percent:
        return RepriceDecision(cost.sku, live_price, implied, margin, target, None, True,
                               f"Within {tolerance_percent}% of target {target}; no change.{note}")

    step = _round(live_price * max_change_percent / HUNDRED)
    if target > live_price:
        proposed = min(target, live_price + step)
    else:
        proposed = max(target, live_price - step, minimum)
    if proposed == live_price:
        return RepriceDecision(cost.sku, live_price, implied, margin, target, None, True,
                               f"No permitted step towards target {target}.{note}")

    within = not note and (floor_price is not None or not require_floor)
    reason = (f"Margin {margin}% over cost {cost.total_cost} gives target {target}; "
              f"live {live_price} -> {proposed}"
              + (" (stepped, max change limit)" if proposed != target else "")
              + "." + note
              + ("" if floor_price is not None or not require_floor else " No price floor set for this SKU."))
    return RepriceDecision(cost.sku, live_price, implied, margin, target, proposed, within, reason)
