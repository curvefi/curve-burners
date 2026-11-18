"""What the price sources share: the source protocol, the USD-feed base
class, the decimals cache and the positive-integer parser."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import Erc20, LotView
from keepers.auction_keeper.rails.common import JsonClient


class PriceSource(Protocol):
    name: str

    def quote(self, lot: LotView) -> int | None:
        """Want amount for ``lot.available`` of ``lot.token``; None when unknown."""
        ...


class Decimals:
    """ERC-20 decimals read once per token."""

    def __init__(self, client: ChainClient):
        self.client = client
        self._cache: dict[str, int] = {}

    def __call__(self, token: str) -> int:
        token = to_checksum_address(token)
        if token not in self._cache:
            self._cache[token] = Erc20(self.client, token).decimals()
        return self._cache[token]


class UsdFeedSource:
    """A feed of USD prices by contract address; the quote converts the lot
    through both tokens' decimals. Subclasses implement ``usd_prices``."""

    name: str

    def __init__(self, chain: str, client: ChainClient, *, http: Any | None = None):
        self.chain = chain
        self.http = http or JsonClient(timeout=15.0)
        self.decimals = Decimals(client)

    def usd_prices(self, tokens: Sequence[str]) -> dict[str, float]:
        """USD price by lowercase address for the tokens the feed knows."""
        raise NotImplementedError

    def quote(self, lot: LotView) -> int | None:
        prices = self.usd_prices([lot.token, lot.want])
        sell_usd = prices.get(lot.token.lower())
        want_usd = prices.get(lot.want.lower())
        if not sell_usd or not want_usd:
            return None
        sell = lot.available / 10 ** self.decimals(lot.token)
        return int(sell * sell_usd / want_usd * 10 ** self.decimals(lot.want))


def positive(value: Any) -> int | None:
    """An amount from an API payload: a positive integer, else None."""
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


__all__ = ["Decimals", "PriceSource", "UsdFeedSource", "positive"]
