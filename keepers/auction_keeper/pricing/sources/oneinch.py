"""1inch swap API quote for the exact lot (Developer Portal key required)."""

from __future__ import annotations

import logging
from typing import Any

from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.pricing.sources.base import positive
from keepers.auction_keeper.rails.common import ApiError, JsonClient

log = logging.getLogger(__name__)


class OneInchQuoteSource:
    """1inch swap API ``/swap/v6.1/{chain}/quote`` with the Developer Portal
    key. UNVERIFIED against the live API (no key at hand): the request shape
    follows the public docs; the answer's ``dstAmount`` is read."""

    name = "oneinch"
    base_url = "https://api.1inch.dev/swap/v6.1"

    def __init__(self, chain_id: int, api_key: str, *, http: Any | None = None):
        self.chain_id = chain_id
        self.api_key = api_key
        self.http = http or JsonClient(timeout=15.0)

    def quote(self, lot: LotView) -> int | None:
        try:
            payload = self.http.request(
                "GET",
                f"{self.base_url}/{self.chain_id}/quote",
                params={"src": lot.token, "dst": lot.want, "amount": str(lot.available)},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except ApiError as exc:
            log.debug("1inch quote unavailable for %s: %s", lot.token, exc)
            return None
        return positive((payload or {}).get("dstAmount"))


__all__ = ["OneInchQuoteSource"]
