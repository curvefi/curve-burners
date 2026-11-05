"""Velora (ParaSwap) ``/prices``: a sell quote for the exact lot (no key)."""

from __future__ import annotations

import logging
from typing import Any

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.pricing.sources.base import Decimals, positive
from keepers.auction_keeper.rails.common import ApiError, JsonClient

log = logging.getLogger(__name__)

# Velora takes the chain id itself; the chains its price API serves.
VELORA_CHAINS = {1, 10, 56, 100, 130, 137, 146, 4663, 8453, 42161, 43114}


class VeloraQuoteSource:
    """Velora (ParaSwap) ``/prices`` sell quote; the network is the chain id."""

    name = "velora"
    base_url = "https://api.paraswap.io"

    def __init__(
        self,
        chain_id: int,
        client: ChainClient,
        *,
        http: Any | None = None,
        partner: str | None = None,
    ):
        self.chain_id = chain_id
        self.http = http or JsonClient(timeout=15.0)
        self.decimals = Decimals(client)
        self.partner = partner

    def quote(self, lot: LotView) -> int | None:
        params = {
            "srcToken": lot.token,
            "destToken": lot.want,
            "amount": str(lot.available),
            "srcDecimals": self.decimals(lot.token),
            "destDecimals": self.decimals(lot.want),
            "side": "SELL",
            "network": self.chain_id,
        }
        if self.partner:
            params["partner"] = self.partner
        try:
            payload = self.http.request("GET", f"{self.base_url}/prices", params=params)
        except ApiError as exc:
            log.debug("velora price unavailable for %s: %s", lot.token, exc)
            return None
        return positive(((payload or {}).get("priceRoute") or {}).get("destAmount"))


__all__ = ["VELORA_CHAINS", "VeloraQuoteSource"]
