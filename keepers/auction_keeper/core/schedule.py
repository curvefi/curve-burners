"""Order validity schedule: dense near the market, sparse away from it.

An order's limit is the curve's quote at posting time, so a standing order
lags the falling curve by at most its validity. Far above the market the
lag costs nothing (the order is unfillable either way); inside the band
around the market it is the delay before the first possible fill. The
schedule therefore spends validity where it matters:

* ``far``   (quote > market * (1 + near_above)): one order valid until the
  curve is predicted to enter the band, found by bisecting the auction's
  own ``getAmountNeeded(token, amount, ts)`` projection; capped by
  ``max_ttl`` so a market that moves re-plans within bounds.
* ``near``  (band): ``min_ttl`` (the orderbook minimum, one or two curve
  steps), reposted as soon as it expires.
* ``below`` (quote < market * (1 - near_below), still unfilled: the market
  estimate was wrong or no one is filling): the uniform ``order_ttl``.
* ``blind`` (no market): the uniform ``order_ttl``, the pre-pricing policy.

Every validity is clipped to the lot window end.
"""

from __future__ import annotations

from dataclasses import dataclass

from keepers.auction_keeper.chain.contracts import Auction, LotView
from keepers.auction_keeper.config import TtlSettings
from keepers.auction_keeper.pricing import MarketQuote

# Bisection resolution for the band entry, in seconds; one node call per halving.
ENTRY_RESOLUTION = 15


@dataclass(frozen=True)
class Plan:
    valid_to: int
    zone: str  # far | near | below | blind
    market: int | None = None
    # far zone: when the curve is predicted to enter the band (lot.end if never).
    band_entry: int | None = None

    def __str__(self) -> str:
        parts = [f"zone={self.zone}", f"valid_to={self.valid_to}"]
        if self.market is not None:
            parts.append(f"market={self.market}")
        if self.band_entry is not None:
            parts.append(f"band_entry={self.band_entry}")
        return " ".join(parts)


def uniform_valid_to(lot: LotView, order_ttl: int) -> int:
    return min(lot.end, lot.timestamp + order_ttl)


def band_entry(auction: Auction, lot: LotView, target: int, lo: int, hi: int) -> int:
    """The first timestamp in [lo, hi) whose quote for the lot is at most
    ``target`` (``hi`` when the curve never gets there), to ENTRY_RESOLUTION."""

    def needed(ts: int) -> int:
        return auction.get_amount_needed(lot.token, lot.available, timestamp=ts)

    if needed(hi - 1) > target:
        return hi
    while hi - lo > ENTRY_RESOLUTION:
        mid = (lo + hi) // 2
        if needed(mid) <= target:
            hi = mid
        else:
            lo = mid
    return hi


def plan(
    auction: Auction,
    lot: LotView,
    market: MarketQuote | None,
    settings: TtlSettings,
    order_ttl: int,
) -> Plan:
    now = lot.timestamp
    if market is None or market.want_amount <= 0:
        return Plan(uniform_valid_to(lot, order_ttl), "blind")
    upper = int(market.want_amount * (1 + settings.near_above))
    lower = int(market.want_amount * (1 - settings.near_below))
    if lot.amount_needed > upper:
        entry = band_entry(auction, lot, upper, now, lot.end)
        valid_to = min(max(entry, now + settings.min_ttl), now + settings.max_ttl, lot.end)
        return Plan(valid_to, "far", market.want_amount, entry)
    if lot.amount_needed >= lower:
        return Plan(min(lot.end, now + settings.min_ttl), "near", market.want_amount)
    return Plan(uniform_valid_to(lot, order_ttl), "below", market.want_amount)


__all__ = ["ENTRY_RESOLUTION", "Plan", "band_entry", "plan", "uniform_valid_to"]
