"""Market pricing for lots: what a lot is worth in want right now, off-chain.

The keeper only needs one number per lot, the want amount the market would
pay for ``lot.available``, to know how far the auction's quote still is from
being fillable. Sources are tried in the configured priority order and the
first answer wins, cross-checked against the next source that has one; a
source that has no price for the pair (a Curve LP token no feed tracks, a
pair the aggregator cannot route, an unsupported chain) answers None and
the next one is asked. With no answer at all the schedule falls back to the
uniform TTL, exactly the pre-pricing behaviour.

Two kinds of source (``pricing/sources/``):

* USD feeds price both tokens in USD and convert through their decimals:
  ``curve`` (prices.curve.finance), ``llama`` (coins.llama.fi),
  ``coingecko`` (CoinGecko token prices by contract address).
* Quote APIs answer the swap itself, lot -> want, for the exact amount:
  ``cow`` (the CoW orderbook ``/quote``, the same quote it later uses for
  its outside-market check), ``kyber`` (KyberSwap aggregator routes),
  ``velora`` (Velora / ParaSwap ``/prices``), ``oneinch`` (1inch swap quote,
  needs the Developer Portal key; UNVERIFIED against the live API),
  ``router`` (the Curve router's own ``get_dy`` over the best route from
  the pool catalogue: on-chain, any chain with Curve pools, and exactly
  what the native rail would be paid), and ``electric`` (the electric
  router's verified quote for its own split route: what the native rail is
  paid through it; one ``erouter`` run per quote).

Adding a source is one module in ``pricing/sources/`` with a class carrying
``name`` and ``quote(lot) -> int | None``, plus a line in ``build_sources``
(and its chain slugs in ``CHAINS``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.config import ConfigError, NetworkConfig
from keepers.auction_keeper.pricing.sources.base import PriceSource, UsdFeedSource
from keepers.auction_keeper.pricing.sources.coingecko import CoinGeckoSource
from keepers.auction_keeper.pricing.sources.cow import CowQuoteSource
from keepers.auction_keeper.pricing.sources.curve import CurvePricesSource
from keepers.auction_keeper.pricing.sources.electric import ElectricRouterSource
from keepers.auction_keeper.pricing.sources.kyber import KyberQuoteSource
from keepers.auction_keeper.pricing.sources.llama import LlamaSource
from keepers.auction_keeper.pricing.sources.oneinch import OneInchQuoteSource
from keepers.auction_keeper.pricing.sources.router import CurveRouterSource
from keepers.auction_keeper.pricing.sources.velora import VELORA_CHAINS, VeloraQuoteSource
from keepers.auction_keeper.rails.cow import ORDERBOOK_URLS
from keepers.auction_keeper.routing.curve_router import curve_router_for
from keepers.auction_keeper.routing.electric_router import electric_router_for

log = logging.getLogger(__name__)

# Chain slugs per source by chain id; override with pricing.chains.<source>.
# The CoW orderbook URLs are the publisher's table (rails/cow.py).
CHAINS: dict[str, dict[int, str]] = {
    "curve": {
        1: "ethereum",
        10: "optimism",
        56: "bsc",
        100: "xdai",
        137: "polygon",
        250: "fantom",
        252: "fraxtal",
        8453: "base",
        42161: "arbitrum",
        43114: "avalanche",
    },
    "llama": {
        1: "ethereum",
        10: "optimism",
        56: "bsc",
        100: "xdai",
        137: "polygon",
        250: "fantom",
        252: "fraxtal",
        8453: "base",
        42161: "arbitrum",
        43114: "avax",
    },
    "coingecko": {
        1: "ethereum",
        10: "optimistic-ethereum",
        56: "binance-smart-chain",
        100: "xdai",
        137: "polygon-pos",
        250: "fantom",
        252: "fraxtal",
        8453: "base",
        42161: "arbitrum-one",
        43114: "avalanche",
    },
    "kyber": {
        1: "ethereum",
        10: "optimism",
        56: "bsc",
        137: "polygon",
        250: "fantom",
        8453: "base",
        42161: "arbitrum",
        43114: "avalanche",
    },
    "cow": ORDERBOOK_URLS,
}


@dataclass(frozen=True)
class MarketQuote:
    """Want the market pays for the lot's available amount, and who said so."""

    want_amount: int
    source: str
    at: float
    # Sources that disagree are all believed: ``want_amount`` is then the
    # highest answer and ``low`` the lowest (None: one estimate).
    low: int | None = None


class MarketPricer:
    """Priority chain over sources with a short per-lot cache (one market
    lookup per lot per ``cache_ttl`` seconds, whatever the poll interval).

    The first source with a price wins, but it is cross-checked against the
    next one that has a price. When the two disagree by more than
    ``cross_check`` (relative to the larger) there is no telling which one
    misprices the token (a feed that does not really cover it answers with
    a stale or near-zero price), so every source is asked and every answer
    believed: the market is the whole span from the lowest to the highest,
    with a warning naming them. The schedule then turns dense as soon as
    the curve nears the most expensive answer and stays dense down to the
    cheapest, so the real market is not slept through whichever it is.
    """

    def __init__(
        self,
        sources: Sequence[PriceSource],
        *,
        cache_ttl: int = 60,
        cross_check: float = 0.5,
        clock: Callable[[], float] = time.time,
    ):
        self.sources = list(sources)
        self.cache_ttl = cache_ttl
        self.cross_check = cross_check
        self.clock = clock
        self._cache: dict[tuple[str, str, int], MarketQuote] = {}

    def _ask(self, source: PriceSource, lot: LotView) -> int | None:
        try:
            amount = source.quote(lot)
        except Exception as exc:  # noqa: BLE001 - a dead feed must not stop publishing
            log.warning("price source %s failed for %s: %s", source.name, lot.token, exc)
            return None
        if amount is None:
            log.debug("price source %s has no market for %s", source.name, lot.token)
        return amount

    def market(self, lot: LotView) -> MarketQuote | None:
        key = (lot.auction.lower(), lot.token.lower(), lot.available)
        now = self.clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached.at < self.cache_ttl:
            return cached
        self._cache.pop(key, None)
        answers: list[tuple[PriceSource, int]] = []
        disagree = False
        for source in self.sources:
            amount = self._ask(source, lot)
            if amount is None:
                continue
            answers.append((source, amount))
            if not self.cross_check:
                break
            if len(answers) == 2:
                (_, first), (_, second) = answers
                disagree = abs(first - second) > self.cross_check * max(first, second)
                if not disagree:
                    break
        if not answers:
            return None
        if disagree:
            log.warning(
                "%s: price sources disagree (%s); believing all of them",
                lot.token,
                ", ".join(f"{source.name}: {amount}" for source, amount in answers),
            )
            high_source, high = max(answers, key=lambda answer: answer[1])
            low = min(amount for _, amount in answers)
            quote = MarketQuote(want_amount=high, source=high_source.name, at=now, low=low)
        else:
            source, amount = answers[0]
            quote = MarketQuote(want_amount=amount, source=source.name, at=now)
        self._cache[key] = quote
        return quote


def _slug(config: NetworkConfig, name: str, chain_id: int) -> str:
    slug = config.pricing.chains.get(name) or CHAINS[name].get(chain_id)
    if slug is None:
        raise ConfigError(
            f"{config.name}: price source {name!r} has no chain slug for chain {chain_id}; "
            f"set pricing.chains.{name} or drop it from pricing.sources"
        )
    return slug


def build_sources(
    config: NetworkConfig, client: ChainClient, *, http: Any | None = None
) -> list[PriceSource]:
    """The network's price sources in priority order (``pricing.sources``).

    A keyed source without its key (1inch) is skipped with a warning rather
    than failing the keeper; a chain a source does not serve is a config
    error, since the slug tables are the documentation of what it covers.
    """
    chain_id = client.chain_id
    pricing = config.pricing
    sources: list[PriceSource] = []
    for name in pricing.sources:
        if name == "curve":
            sources.append(CurvePricesSource(_slug(config, name, chain_id), client, http=http))
        elif name == "llama":
            sources.append(LlamaSource(_slug(config, name, chain_id), client, http=http))
        elif name == "coingecko":
            sources.append(
                CoinGeckoSource(
                    _slug(config, name, chain_id),
                    client,
                    http=http,
                    api_key=pricing.coingecko_api_key,
                    base_url=pricing.coingecko_url,
                )
            )
        elif name == "cow":
            url = config.cow.orderbook_url or CHAINS["cow"].get(chain_id)
            if url is None:
                raise ConfigError(
                    f"{config.name}: no CoW orderbook for chain {chain_id}; "
                    "set cow.orderbook_url or drop 'cow' from pricing.sources"
                )
            sources.append(CowQuoteSource(url, http=http))
        elif name == "kyber":
            sources.append(KyberQuoteSource(_slug(config, name, chain_id), http=http))
        elif name == "velora":
            if chain_id not in VELORA_CHAINS:
                raise ConfigError(
                    f"{config.name}: Velora does not serve chain {chain_id}; "
                    "drop 'velora' from pricing.sources"
                )
            sources.append(VeloraQuoteSource(chain_id, client, http=http))
        elif name == "router":
            sources.append(CurveRouterSource(curve_router_for(config, client, http=http)))
        elif name == "electric":
            sources.append(ElectricRouterSource(electric_router_for(config, client)))
        elif name == "oneinch":
            key = pricing.oneinch_api_key
            if not key:
                log.warning(
                    "%s: price source oneinch needs $%s; skipped",
                    config.name,
                    pricing.oneinch_api_key_env,
                )
                continue
            sources.append(OneInchQuoteSource(chain_id, key, http=http))
        else:  # pragma: no cover - rejected at config parse time
            raise ConfigError(f"{config.name}: unknown price source {name!r}")
    return sources


__all__ = [
    "CHAINS",
    "CoinGeckoSource",
    "CowQuoteSource",
    "CurvePricesSource",
    "CurveRouterSource",
    "ElectricRouterSource",
    "KyberQuoteSource",
    "LlamaSource",
    "MarketPricer",
    "MarketQuote",
    "OneInchQuoteSource",
    "PriceSource",
    "UsdFeedSource",
    "VeloraQuoteSource",
    "build_sources",
]
