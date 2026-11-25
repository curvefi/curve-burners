"""Order validity schedule: dense around every price level, sparse elsewhere.

An order's limit is the curve's quote at posting time, so a standing order
lags the falling curve by at most its validity. Away from any price the lag
costs nothing (the order is unfillable either way); around a price it is
the delay before the first possible fill. Every source's answer is such a
price (``MarketQuote.levels``): any of them may be the one that knows the
route, so each gets its own band, from ``near_above`` over it down to
``near_below`` under it, and the schedule spends validity inside the bands:

* ``near``  (quote inside the band of some level): ``min_ttl`` (the
  orderbook minimum, one or two curve steps), reposted as soon as it
  expires.
* ``far``   (quote above a band it has not reached yet: over the highest
  level, or in a gap between two levels): one order valid until the curve
  enters the next band down (``reaches``: the local model's answer, no node
  call); capped by ``max_ttl`` so a market that moves re-plans within
  bounds.
* ``below`` (quote under every band, still unfilled: the estimates were
  wrong or no one is filling): the uniform ``order_ttl``.
* ``blind`` (no market): the uniform ``order_ttl``, the pre-pricing policy.

Every validity is clipped to the lot window end.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.config import TtlSettings
from keepers.auction_keeper.pricing import MarketQuote

ZONES = ("near", "below", "blind", "far")


@dataclass(frozen=True)
class Plan:
    valid_to: int
    zone: str  # one of ZONES
    # The level the lot is at (near), heading for (far), or the lowest (below).
    market: int | None = None
    # far zone: when the curve is predicted to enter the next band (lot.end if never).
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


def bands(market: MarketQuote, settings: TtlSettings) -> list[tuple[int, int, int]]:
    """The dense band around each price level, highest first: (low, high, level)."""
    return [
        (int(level * (1 - settings.near_below)), int(level * (1 + settings.near_above)), level)
        for level in market.prices
        if level > 0
    ]


def locate(
    ask: int, market: MarketQuote | None, settings: TtlSettings
) -> tuple[str, int | None, int | None]:
    """Where an ask stands against the price levels: the zone (see the module
    docstring), the level it belongs to, and for ``far`` the upper edge of
    the band the curve enters next."""
    if market is None or market.want_amount <= 0:
        return "blind", None, None
    around = bands(market, settings)
    for low, high, level in around:
        if low <= ask <= high:
            return "near", level, None
    ahead = [(high, level) for _, high, level in around if high < ask]
    if ahead:
        high, level = max(ahead)
        return "far", level, high
    return "below", around[-1][2], None


def zone_of(ask: int, market: MarketQuote | None, settings: TtlSettings) -> str:
    return locate(ask, market, settings)[0]


def plan(
    lot: LotView,
    market: MarketQuote | None,
    settings: TtlSettings,
    order_ttl: int,
    *,
    reaches: Callable[[int], int | None] | None = None,
) -> Plan:
    """The lot's zone and order validity. ``reaches(target)`` answers when
    the lot's quote first gets down to ``target`` want (None: never, or not
    known), which sizes the one long order of the ``far`` zone."""
    now = lot.timestamp
    zone, level, upper = locate(lot.amount_needed, market, settings)
    if zone == "blind":
        return Plan(uniform_valid_to(lot, order_ttl), "blind")
    if zone == "far":
        assert upper is not None
        entry = (reaches(upper) if reaches is not None else None) or lot.end
        valid_to = min(max(entry, now + settings.min_ttl), now + settings.max_ttl, lot.end)
        return Plan(valid_to, "far", level, entry)
    if zone == "near":
        return Plan(min(lot.end, now + settings.min_ttl), "near", level)
    return Plan(uniform_valid_to(lot, order_ttl), "below", level)


__all__ = ["ZONES", "Plan", "bands", "locate", "plan", "uniform_valid_to", "zone_of"]
