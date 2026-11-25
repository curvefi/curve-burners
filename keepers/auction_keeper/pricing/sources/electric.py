"""The electric router's verified quote for its own split route: what the
native rail is paid through it; one ``erouter`` run per quote."""

from __future__ import annotations

from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.routing.electric_router import ElectricRouter


class ElectricRouterSource:
    """The electric router's verified quote for the whole lot (off-chain solve)."""

    name = "electric"

    def __init__(self, router: ElectricRouter):
        self.router = router

    def quote(self, lot: LotView) -> int | None:
        best = self.router.best_route(lot.token, lot.want, lot.available)
        return best[1] if best else None


__all__ = ["ElectricRouterSource"]
