"""The Curve router's own quote over the best catalogued route (on-chain, no
feed, no key: exactly what the native rail would be paid)."""

from __future__ import annotations

from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.routing.curve_router import CurveRouter


class CurveRouterSource:
    """The Curve router's quote over the best catalogued route (on-chain)."""

    name = "router"

    def __init__(self, router: CurveRouter):
        self.router = router

    def quote(self, lot: LotView) -> int | None:
        best = self.router.best_route(lot.token, lot.want, lot.available)
        return best[1] if best else None


__all__ = ["CurveRouterSource"]
