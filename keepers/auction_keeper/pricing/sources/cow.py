"""The CoW orderbook's own sell quote for the lot (``/api/v1/quote``)."""

from __future__ import annotations

import logging
from typing import Any

from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.pricing.sources.base import positive
from keepers.auction_keeper.rails.common import ApiError, JsonClient

log = logging.getLogger(__name__)


class CowQuoteSource:
    """The CoW orderbook's own sell quote for the lot, from the auction."""

    name = "cow"

    def __init__(self, orderbook_url: str, *, http: Any | None = None):
        self.orderbook_url = orderbook_url.rstrip("/")
        self.http = http or JsonClient(timeout=15.0)

    def quote(self, lot: LotView) -> int | None:
        body = {
            "sellToken": lot.token,
            "buyToken": lot.want,
            "receiver": lot.receiver,
            "from": lot.auction,
            "kind": "sell",
            "sellAmountBeforeFee": str(lot.available),
            "signingScheme": "eip1271",
            "onchainOrder": False,
            "priceQuality": "fast",
        }
        try:
            payload = self.http.request("POST", f"{self.orderbook_url}/api/v1/quote", json=body)
        except ApiError as exc:
            # NoLiquidity, UnsupportedToken, SellAmountDoesNotCoverFee: no market here.
            log.debug("cow quote unavailable for %s: %s", lot.token, exc)
            return None
        return positive(((payload or {}).get("quote") or {}).get("buyAmount"))


__all__ = ["CowQuoteSource"]
