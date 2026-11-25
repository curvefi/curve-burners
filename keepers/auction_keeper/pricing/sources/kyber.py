"""KyberSwap aggregator routes: a swap quote for the exact lot (no key)."""

from __future__ import annotations

import logging
from typing import Any

from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.pricing.sources.base import positive
from keepers.auction_keeper.rails.common import ApiError, JsonClient

log = logging.getLogger(__name__)


class KyberQuoteSource:
    """KyberSwap aggregator ``/{chain}/api/v1/routes`` (no key)."""

    name = "kyber"
    base_url = "https://aggregator-api.kyberswap.com"

    def __init__(self, chain: str, *, http: Any | None = None, client_id: str | None = None):
        self.chain = chain
        self.http = http or JsonClient(timeout=15.0)
        self.client_id = client_id

    def quote(self, lot: LotView) -> int | None:
        try:
            payload = self.http.request(
                "GET",
                f"{self.base_url}/{self.chain}/api/v1/routes",
                params={"tokenIn": lot.token, "tokenOut": lot.want, "amountIn": str(lot.available)},
                headers={"x-client-id": self.client_id} if self.client_id else None,
            )
        except ApiError as exc:
            log.debug("kyber route unavailable for %s: %s", lot.token, exc)
            return None
        if (payload or {}).get("code") not in (0, None):
            log.debug("kyber: %s for %s", payload.get("message"), lot.token)
            return None
        summary = ((payload or {}).get("data") or {}).get("routeSummary") or {}
        return positive(summary.get("amountOut"))


__all__ = ["KyberQuoteSource"]
