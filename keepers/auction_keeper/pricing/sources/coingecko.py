"""CoinGecko: USD token prices by contract address (demo or pro API)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.pricing.sources.base import UsdFeedSource


class CoinGeckoSource(UsdFeedSource):
    """CoinGecko ``/simple/token_price/{platform}``; the demo API without a
    key is heavily rate-limited, so a key (``pricing.coingecko_api_key_env``)
    is recommended. A pro key goes with the pro-api base URL."""

    name = "coingecko"

    def __init__(
        self,
        chain: str,
        client: ChainClient,
        *,
        http: Any | None = None,
        api_key: str | None = None,
        base_url: str = "https://api.coingecko.com/api/v3",
    ):
        super().__init__(chain, client, http=http)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def usd_prices(self, tokens: Sequence[str]) -> dict[str, float]:
        headers = {}
        if self.api_key:
            pro = "pro-api" in self.base_url
            headers["x-cg-pro-api-key" if pro else "x-cg-demo-api-key"] = self.api_key
        payload = self.http.request(
            "GET",
            f"{self.base_url}/simple/token_price/{self.chain}",
            params={
                "contract_addresses": ",".join(t.lower() for t in tokens),
                "vs_currencies": "usd",
            },
            headers=headers or None,
        )
        prices: dict[str, float] = {}
        for address, entry in (payload or {}).items():
            price = (entry or {}).get("usd")
            if price and price > 0:
                prices[address.lower()] = float(price)
        return prices


__all__ = ["CoinGeckoSource"]
