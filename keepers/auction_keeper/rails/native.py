"""The native rail: fill a lot through the AuctionTaker with a Curve swap.

Where no settlement adapter is active (or alongside them), the keeper can
take a lot itself: the ``AuctionTaker`` receives the lot, swaps it to want
on Curve through the router, pays the auction's quote from the proceeds and
sends the rest to the keeper. It needs only gas, and it works on any chain
with Curve pools.

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
from dataclasses import dataclass

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import Auction, LotView
from keepers.auction_keeper.config import NetworkConfig
from keepers.auction_keeper.execute import Executor, TakePlan, approve_call
from keepers.auction_keeper.routing.curve_router import CurveRouter, Route

log = logging.getLogger(__name__)

# Chunk search: the small probe amount as a fraction of the lot, and the
# bisection steps (each one router call).
PROBE_FRACTION = 1000
CHUNK_STEPS = 8


@dataclass(frozen=True)
class TakeQuote:
    route: Route
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
        router: CurveRouter,
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

    def chunk(self, route: Route, available: int) -> tuple[int, int, float] | None:
        """The largest amount up to ``available`` whose impact stays under
        ``max_impact``: (amount, dy, impact); None when the route pays nothing."""
        probe = max(available // PROBE_FRACTION, 1)
        probe_dy = self.router.get_dy(route, probe)
        if probe_dy <= 0:
            return None
        unit = probe_dy / probe

        def impact_of(amount: int) -> tuple[int, float]:
            dy = self.router.get_dy(route, amount)
            return dy, 1.0 - (dy / amount) / unit if dy else 1.0

        dy, impact = impact_of(available)
        if impact <= self.settings.max_impact:
            return available, dy, impact
        lo, hi = probe, available  # impact(lo) ~ 0 by construction
        best = (probe, probe_dy, 0.0)
        for _ in range(CHUNK_STEPS):
            mid = (lo + hi) // 2
            if mid <= lo:
                break
            dy, impact = impact_of(mid)
            if impact <= self.settings.max_impact:
                best, lo = (mid, dy, impact), mid
            else:
                hi = mid
        return best

    def quote(self, auction: Auction, lot: LotView) -> TakeQuote | None:
        best = self.router.best_route(lot.token, lot.want, lot.available)
        if best is None:
            return None
        route, _ = best
        chunked = self.chunk(route, lot.available)
        if chunked is None:
            return None
        amount, dy, impact = chunked
        payment = auction.get_amount_needed(lot.token, amount)
        return TakeQuote(route, amount, dy, payment, impact)

    # Execution

    def attempt(self, auction: Auction, lot: LotView, *, dry_run: bool) -> NativeResult:
        """One try at the lot: take (or simulate) when the quote is under the route."""
        try:
            quote = self.quote(auction, lot)
        except Exception as exc:  # noqa: BLE001 - a dead API or router must not stop the loop
            log.warning("%s: native quote failed: %s", lot.token, exc)
            return NativeResult("failed", f"quote: {exc}")
        if quote is None:
            return NativeResult("unroutable", "no Curve route pays for the lot")
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
            route=[
                approve_call(lot.token, self.router.address, quote.amount),
                self.router.exchange_call(
                    quote.route, quote.amount, quote.payment, self.executor.taker.address
                ),
            ],
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


__all__ = ["NativeResult", "NativeTaker", "TakeQuote"]
