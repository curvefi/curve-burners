"""The native rail: fill a lot through the AuctionTaker with a router swap.

Where no settlement adapter is active (or alongside them), the keeper can
take a lot itself: the ``AuctionTaker`` receives the lot, swaps it to want
through a router, pays the auction's quote from the proceeds and sends the
rest to the keeper. It needs only gas, and it works on any chain with Curve
pools.

The routers are ``native.backends`` (``BACKENDS`` below): ``curve`` is the
Curve router NG over routes found from the pool catalogue
(``keepers.auction_keeper.routing.curve_router``), ``electric`` is the
electric router, which solves splits over the whole pool graph off-chain
(``keepers.auction_keeper.routing.electric_router``). With several, every
lot is quoted on all of them at once and swapped through the one that pays
more, so a lot one cannot route, or a router that is down, costs nothing.

Each cycle, per live lot, the taker

1. finds the best router route for the whole available amount;
2. shrinks the take to the largest chunk whose price impact on that route
   stays under ``native.max_impact``: a thin pool is hit in pieces, and
   arbitrage refills it between pieces, so the lot clears nearer the price
   of a deep market than a single dump would;
3. takes the chunk when the auction's quote for it is at least
   ``native.margin`` under the route's quote, simulating the call first.

The keeper polls faster (``native.poll``) while a lot's quote is within
the schedule's band above the route quote.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import Auction, LotView
from keepers.auction_keeper.config import NetworkConfig
from keepers.auction_keeper.execute import Call, Executor, TakePlan
from keepers.auction_keeper.routing.curve_router import curve_router_for
from keepers.auction_keeper.routing.electric_router import electric_router_for

log = logging.getLogger(__name__)


class Router(Protocol):
    """What the rail needs from a router; a route is the router's own type."""

    def best_route(self, token: str, want: str, amount: int) -> tuple[Any, int] | None:
        """The best route for ``amount`` and the want it pays; None when nothing routes."""

    def chunk(
        self, route: Any, available: int, max_impact: float
    ) -> tuple[Any, int, int, float] | None:
        """The largest amount up to ``available`` whose price impact stays under
        ``max_impact``: (route, amount, dy, impact); None when the route pays nothing."""

    def swap_calls(self, route: Any, amount: int, min_dy: int, receiver: str) -> list[Call]:
        """The AuctionTaker's route steps swapping ``amount`` to ``receiver``."""


# The routers by name (``native.backends``). Adding one is a module with the
# ``Router`` methods, a line here and its name in config.native.NATIVE_BACKENDS.
BACKENDS: dict[str, Callable[[NetworkConfig, ChainClient], Router]] = {
    "curve": curve_router_for,
    "electric": electric_router_for,
}


@dataclass(frozen=True)
class Routed:
    """A route with the router it belongs to, and the other routers' answers
    for the same lot, best first, to fall back on."""

    router: Any
    route: Any
    others: tuple[tuple[Any, Any], ...] = ()

    def __str__(self) -> str:
        return str(self.route)


class BestRouter:
    """Several routers asked for every lot; the one paying the most swaps it.

    The routers are independent: one that fails (the electric router's tool
    missing or timing out, a catalogue that does not load) is logged and
    left out of that quote, and when the winner cannot chunk the lot the
    next best answer is used. They are asked at once (``parallel``), so the
    quote takes as long as the slowest of them, not their sum."""

    def __init__(self, routers: Sequence[Router], *, parallel: bool = True):
        self.routers = list(routers)
        self._pool = (
            ThreadPoolExecutor(len(self.routers), thread_name_prefix="router")
            if parallel and len(self.routers) > 1
            else None
        )

    @staticmethod
    def _ask(router: Router, token: str, want: str, amount: int) -> tuple[Any, int] | None:
        try:
            return router.best_route(token, want, amount)
        except Exception as exc:  # noqa: BLE001 - the other routers still answer
            log.warning("%s failed for %s: %s", type(router).__name__, token, exc)
            return None

    def best_route(self, token: str, want: str, amount: int) -> tuple[Routed, int] | None:
        def ask(router: Router) -> tuple[Any, int] | None:
            return self._ask(router, token, want, amount)

        answers = list(self._pool.map(ask, self.routers) if self._pool else map(ask, self.routers))
        found = [(router, *answer) for router, answer in zip(self.routers, answers) if answer]
        if not found:
            return None
        found.sort(key=lambda item: -item[2])
        (router, route, dy), *rest = found
        return Routed(router, route, tuple((other, alt) for other, alt, _ in rest)), dy

    def chunk(
        self, route: Routed, available: int, max_impact: float
    ) -> tuple[Routed, int, int, float] | None:
        for router, inner in ((route.router, route.route), *route.others):
            try:
                chunked = router.chunk(inner, available, max_impact)
            except Exception as exc:  # noqa: BLE001 - the next best router is tried
                log.warning("%s failed to chunk %s: %s", type(router).__name__, inner, exc)
                continue
            if chunked is not None:
                inner, amount, dy, impact = chunked
                return Routed(router, inner), amount, dy, impact
        return None

    def swap_calls(self, route: Routed, amount: int, min_dy: int, receiver: str) -> list[Call]:
        return route.router.swap_calls(route.route, amount, min_dy, receiver)


def router_for(config: NetworkConfig, client: ChainClient) -> Router:
    """The router of the network's ``native.backends``: the one named, or all
    of them behind a ``BestRouter``."""
    routers = [BACKENDS[name](config, client) for name in config.native.backends]
    if len(routers) == 1:
        return routers[0]
    return BestRouter(routers, parallel=getattr(client, "concurrent", False))


@dataclass(frozen=True)
class TakeQuote:
    route: Any  # the router's own route for the chunk
    amount: int  # the chunk to take
    dy: int  # want the route pays for the chunk
    payment: int  # want the auction asks for the chunk
    impact: float  # the route's price impact for the chunk

    @property
    def ratio(self) -> float:
        return self.payment / self.dy if self.dy else float("inf")

    def __str__(self) -> str:
        return (
            f"chunk={self.amount} payment={self.payment} route_dy={self.dy} "
            f"impact={self.impact:.4f} via {self.route}"
        )


@dataclass(frozen=True)
class NativeResult:
    action: str  # taken | simulated | waiting | unroutable | failed
    detail: str = ""
    near: bool = False  # the quote is within the band above the route quote


class NativeTaker:
    def __init__(
        self,
        config: NetworkConfig,
        client: ChainClient,
        router: Router,
        *,
        taker: str,
        profit_receiver: str | None = None,
    ):
        self.config = config
        self.settings = config.native
        self.router = router
        self.executor = Executor(client, taker=taker)
        self.profit_receiver = profit_receiver

    # Quotes

    def quote(self, auction: Auction, lot: LotView) -> TakeQuote | None:
        best = self.router.best_route(lot.token, lot.want, lot.available)
        if best is None:
            return None
        route, _ = best
        chunked = self.router.chunk(route, lot.available, self.settings.max_impact)
        if chunked is None:
            return None
        route, amount, dy, impact = chunked
        payment = auction.get_amount_needed(lot.token, amount)
        return TakeQuote(route, amount, dy, payment, impact)

    # Execution

    def try_quote(self, auction: Auction, lot: LotView) -> TakeQuote | NativeResult:
        """The quote, or the result the lot ends with when there is none. Only
        reads, so the keeper calls it for several lots at once."""
        try:
            quote = self.quote(auction, lot)
        except Exception as exc:  # noqa: BLE001 - a dead API or router must not stop the loop
            log.warning("%s: native quote failed: %s", lot.token, exc)
            return NativeResult("failed", f"quote: {exc}")
        if quote is None:
            return NativeResult("unroutable", "no route pays for the lot")
        return quote

    def attempt(
        self,
        auction: Auction,
        lot: LotView,
        *,
        dry_run: bool,
        quoted: TakeQuote | NativeResult | None = None,
    ) -> NativeResult:
        """One try at the lot: take (or simulate) when the quote is under the
        route. ``quoted`` is a ``try_quote`` answer made ahead for this lot."""
        quote = quoted if quoted is not None else self.try_quote(auction, lot)
        if isinstance(quote, NativeResult):
            return quote
        near = quote.payment <= quote.dy * (1 + self.config.ttl.near_above)
        if quote.payment > quote.dy * (1 - self.settings.margin):
            log.info("%s: waiting, %s", lot.token, quote)
            return NativeResult("waiting", str(quote), near)
        sender = self.executor.client.sender
        receiver = self.profit_receiver or self.settings.profit_receiver or sender
        if receiver is None:
            return NativeResult("failed", "no sender and no native.profit_receiver")
        plan = TakePlan(
            auction.address,
            lot.token,
            max_amount=quote.amount,
            route=self.router.swap_calls(
                quote.route, quote.amount, quote.payment, self.executor.taker.address
            ),
            min_profit=0,
            profit_receiver=to_checksum_address(receiver),
        )
        try:
            result = self.executor.take_with_route(plan, timeout=0, dry_run=dry_run)
        except CallReverted as exc:
            log.warning("%s: take reverted in simulation: %s", lot.token, exc)
            return NativeResult("failed", f"simulation: {exc}", near)
        except Exception as exc:  # noqa: BLE001 - report, keep the loop alive
            log.exception("%s: native take failed", lot.token)
            return NativeResult("failed", str(exc), near)
        if result is None:
            log.info("%s: dry run, would take %s", lot.token, quote)
            return NativeResult("simulated", str(quote), near)
        log.info("%s: TAKEN tx %s, %s", lot.token, result.tx_hash, quote)
        return NativeResult("taken", f"tx {result.tx_hash} {quote}", near)


__all__ = [
    "BACKENDS",
    "BestRouter",
    "NativeResult",
    "NativeTaker",
    "Routed",
    "Router",
    "TakeQuote",
    "router_for",
]
