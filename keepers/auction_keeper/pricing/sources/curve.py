"""prices.curve.finance: USD prices by token address."""

from __future__ import annotations

from collections.abc import Sequence

from keepers.auction_keeper.pricing.sources.base import UsdFeedSource
from keepers.auction_keeper.rails.common import ApiError


class CurvePricesSource(UsdFeedSource):
    """prices.curve.finance ``/v1/usd_price/{chain}/{token}``."""

    name = "curve"
    base_url = "https://prices.curve.finance"

    def usd_prices(self, tokens: Sequence[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for token in tokens:
            url = f"{self.base_url}/v1/usd_price/{self.chain}/{token.lower()}"
            try:
                payload = self.http.request("GET", url)
            except ApiError as exc:
                if exc.status == 404:
                    continue
                raise
            price = ((payload or {}).get("data") or {}).get("usd_price")
            if price and price > 0:
                prices[token.lower()] = float(price)
        return prices


__all__ = ["CurvePricesSource"]
