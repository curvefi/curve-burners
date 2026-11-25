"""Which lots a cycle works on: the ranking pass and the selection.

``rank`` goes over every lot of an auction with nothing but the local model
(``core.model``) and the remembered market estimates: no node call, no HTTP.
It answers, per live lot, the ask at the pricing timestamp and how far that
is above the highest estimate. ``select`` then picks the lots that get real
work this cycle (a chain read, quotes, orders, takes):

* every lot within ``focus.threshold`` of its market, however many (lots of
  one window share a curve, so many can arrive at once);
* the ``focus.nearest`` nearest of the rest;
* ``focus.blind`` lots no source prices, in rotation;

at most ``focus.max_lots`` in all, nearest first. A lot that is sold out,
outside its window or dust is not ranked at all, so it costs nothing for
the rest of the window. ``stale`` lists the estimates worth asking for
again, most urgent first.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from keepers.auction_keeper.config import FocusSettings, PricingSettings
from keepers.auction_keeper.core.model import Curve, LotRecord, mul_div_up
from keepers.auction_keeper.pricing import MarketQuote


@dataclass(frozen=True)
class Ranked:
    record: LotRecord
    # Want asked for everything available at the pricing timestamp.
    ask: int
    market: MarketQuote | None
    # ask / highest estimate (None: no estimate). 1.0 is the market.
    ratio: float | None

    @property
    def token(self) -> str:
        return self.record.token


def rank(
    records: Iterable[LotRecord],
    curve: Curve,
    timestamp: int,
    estimate: Callable[[LotRecord], MarketQuote | None],
    *,
    dust: float = 0,
) -> list[Ranked]:
    """Every lot live at ``timestamp``, nearest its market first, the lots
    without an estimate last. A lot whose highest estimate is under ``dust``
    want wei is left out."""
    totals: dict[int, int] = {}  # the lots of a window share the curve
    ranked: list[Ranked] = []
    for record in records:
        if not record.active(timestamp):
            continue
        total = totals.get(record.start)
        if total is None:
            total = totals[record.start] = curve.total_price(timestamp - record.start)
        ask = mul_div_up(total, record.available, record.initial_amount)
        market = estimate(record)
        if market is None or market.want_amount <= 0:
            ranked.append(Ranked(record, ask, None, None))
            continue
        if market.want_amount < dust:
            continue
        ranked.append(Ranked(record, ask, market, ask / market.want_amount))
    ranked.sort(key=lambda item: (item.ratio is None, item.ratio or 0.0, item.token))
    return ranked


def select(ranked: list[Ranked], settings: FocusSettings, *, rotation: int = 0) -> list[Ranked]:
    """The lots to work on this cycle (see the module docstring). ``rotation``
    moves through the unpriced lots from one cycle to the next."""
    priced = [item for item in ranked if item.ratio is not None]
    blind = [item for item in ranked if item.ratio is None]
    turn = min(settings.blind, len(blind), settings.max_lots)
    edge = 1 + settings.threshold
    within = sum(1 for item in priced if item.ratio <= edge)  # type: ignore[operator]
    chosen = priced[: min(within + settings.nearest, settings.max_lots - turn)]
    for index in range(turn):
        chosen.append(blind[(rotation * settings.blind + index) % len(blind)])
    return chosen


def seconds_to_fall(curve: Curve, factor: float) -> float:
    """Roughly how long the curve takes to fall by ``factor`` (> 1) while it
    is decaying; 0 for a factor it is already at."""
    if factor <= 1 or not curve.log_drop:
        return 0.0
    steps = math.log(factor) * 10**18 / curve.log_drop * curve.decay_steps
    return steps * curve.step_duration


def estimate_ttl(
    item: Ranked, curve: Curve, focus: FocusSettings, pricing: PricingSettings
) -> float:
    """How old the lot's estimate may get: ``pricing.cache`` once the lot is
    within reach of the focus threshold, up to ``pricing.cache_far`` before,
    and never more than half the time the curve needs to get there."""
    if item.ratio is None:
        return pricing.cache_far
    away = seconds_to_fall(curve, item.ratio / (1 + focus.threshold))
    return min(pricing.cache_far, max(pricing.cache, away / 2))


def stale(
    ranked: list[Ranked],
    curve: Curve,
    age: Callable[[LotRecord], float | None],
    focus: FocusSettings,
    pricing: PricingSettings,
) -> list[Ranked]:
    """The lots whose estimate is due to be asked for again, most urgent
    first: the ones near their market, then the ones never asked about (they
    cannot be ranked without an estimate), then the far ones."""
    urgent, never, due = [], [], []
    for item in ranked:
        seen = age(item.record)
        if seen is None:
            never.append(item)
            continue
        ttl = estimate_ttl(item, curve, focus, pricing)
        if seen >= ttl:
            (urgent if ttl <= pricing.cache else due).append(item)
    return urgent + never + due


__all__ = ["Ranked", "estimate_ttl", "rank", "seconds_to_fall", "select", "stale"]
