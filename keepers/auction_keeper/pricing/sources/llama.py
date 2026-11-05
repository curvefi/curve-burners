"""coins.llama.fi (DefiLlama): USD prices by chain-qualified token address."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from keepers.auction_keeper.pricing.sources.base import UsdFeedSource

log = logging.getLogger(__name__)

# coins.llama.fi answers with a confidence in [0, 1]; lower is a guess.
LLAMA_MIN_CONFIDENCE = 0.8


class LlamaSource(UsdFeedSource):
    """coins.llama.fi ``/prices/current/{chain}:{token},...``."""

    name = "llama"
    base_url = "https://coins.llama.fi"

    def usd_prices(self, tokens: Sequence[str]) -> dict[str, float]:
        keys = ",".join(f"{self.chain}:{token.lower()}" for token in tokens)
        payload = self.http.request("GET", f"{self.base_url}/prices/current/{keys}")
        prices: dict[str, float] = {}
        for key, coin in ((payload or {}).get("coins") or {}).items():
            price = coin.get("price")
            if not price or price <= 0:
                continue
            if coin.get("confidence", 1.0) < LLAMA_MIN_CONFIDENCE:
                log.debug("llama: low confidence %s for %s", coin.get("confidence"), key)
                continue
            prices[key.split(":", 1)[1].lower()] = float(price)
        return prices


__all__ = ["LLAMA_MIN_CONFIDENCE", "LlamaSource"]
