"""Market pricing for lots: what a lot is worth in want right now, off-chain.

The keeper needs one number per lot, the want amount the market would pay
for what is left of it, to know how far the auction's quote still is from
being fillable. Every source is asked and every answer is kept as a price
level of its own (``MarketPricer``): the schedule posts densely around each
of them and sparsely in between. A source that has no price for the pair (a
Curve LP token no feed tracks, a pair the aggregator cannot route, an
unsupported chain) answers None and adds no level. Estimates are kept for a
long time and scaled to the lot's remaining amount, so the cycle reads them
from memory. With no answer at all the schedule falls back to the uniform
TTL.

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
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.chain.contracts import LotView
from keepers.auction_keeper.config import ConfigError, NetworkConfig
from keepers.auction_keeper.limits import Throttled
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
    """Want the market pays for ``amount`` of the lot token, and who said so."""

    # The highest answer: the estimate the lot is ranked against.
    want_amount: int
    source: str
    at: float
    # Every source's answer, highest first: (source, want). Each one is a
    # price level the schedule works densely around. Empty: ``want_amount``
    # is the only level.
    levels: tuple[tuple[str, int], ...] = ()
    # The lot amount the answers were given for (0: not recorded).
    amount: int = 0

    @property
    def prices(self) -> tuple[int, ...]:
        """The levels' want amounts, highest first."""
        return tuple(want for _, want in self.levels) or (self.want_amount,)

    def scaled(self, amount: int) -> MarketQuote:
        """The estimate for another amount of the token, pro rata."""
        if not self.amount or amount == self.amount:
            return self
        return replace(
            self,
            want_amount=self.want_amount * amount // self.amount,
            levels=tuple((name, want * amount // self.amount) for name, want in self.levels),
            amount=amount,
        )

    def with_level(self, source: str, want: int, at: float) -> MarketQuote:
        """The estimate with ``source``'s level set to ``want``."""
        levels = self.levels or ((self.source, self.want_amount),)
        return _quote(
            [*(level for level in levels if level[0] != source), (source, want)], at, self.amount
        )


def _quote(answers: list[tuple[str, int]], at: float, amount: int) -> MarketQuote:
    ranked = tuple(sorted(answers, key=lambda answer: -answer[1]))
    source, high = ranked[0]
    return MarketQuote(high, source, at, levels=ranked, amount=amount)


class MarketPricer:
    """Every source's estimate of a lot, kept for a long time.

    An estimate is asked for once and then reused: ``cached`` answers from
    memory, scaled to what is left of the lot, and ``refresh`` asks the
    sources again. How old an estimate may get is the caller's decision
    (``core.focus``: an hour far from the market, a minute near it), so a
    lookup never stands between the keeper and a lot it has to act on.

    How the sources are combined:

    * Every source is asked. One with no price (a Curve LP token the feed
      does not track, a pair the aggregator cannot route) adds nothing.
    * Every answer is kept as a price level (``MarketQuote.levels``). No
      source outvotes another: any of them may be the one that knows the
      route, so the schedule (``core.schedule``) posts densely inside the
      band around each level and sparsely between the bands.
    * The highest answer (``want_amount``) is what the lot is ranked
      against, so a lot comes into focus before its first level.
    """

    def __init__(
        self,
        sources: Sequence[PriceSource],
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.sources = list(sources)
        self.clock = clock
        # (auction, token) -> the last estimate; None when no source had one.
        self._cache: dict[tuple[str, str], MarketQuote | None] = {}
        self._asked: dict[tuple[str, str], float] = {}
        # When each source last answered about the token (a price or none).
        self._answered: dict[tuple[str, str], dict[str, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(auction: str, token: str) -> tuple[str, str]:
        return auction.lower(), token.lower()

    def _ask(self, source: PriceSource, lot: LotView) -> int | None:
        try:
            amount = source.quote(lot)
        except Throttled:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead feed must not stop publishing
            log.warning("price source %s failed for %s: %s", source.name, lot.token, exc)
            return None
        if amount is None:
            log.debug("price source %s has no market for %s", source.name, lot.token)
            return None
        return amount if amount > 0 else None

    def combine(self, lot: LotView, answers: list[tuple[str, int]]) -> MarketQuote | None:
        """The estimate from the sources' answers (see the class docstring)."""
        if not answers:
            return None
        log.debug(
            "%s: price levels %s",
            lot.token,
            ", ".join(f"{name}: {amount}" for name, amount in answers),
        )
        return _quote(answers, self.clock(), lot.available)

    def refresh(self, lot: LotView, *, fresh: float = 0.0) -> MarketQuote | None:
        """Ask the sources about the lot and remember the answers.

        A source that answered less than ``fresh`` seconds ago is not asked
        again, and one whose request budget is spent (``Throttled``) is
        skipped: both keep the level they gave before. The lot counts as
        asked about (``age``) only once every source has had its say, so a
        throttled source is come back to on a later cycle, and then it is
        the only one asked."""
        key = self._key(lot.auction, lot.token)
        now = self.clock()
        with self._lock:
            known = self._cache.get(key)
            answered = dict(self._answered.get(key, {}))
        kept: dict[str, int] = {}
        if known is not None:
            known = known.scaled(lot.available)
            kept = dict(known.levels or ((known.source, known.want_amount),))
        answers: list[tuple[str, int]] = []
        complete = True
        for source in self.sources:
            name = source.name
            last = answered.get(name)
            if fresh and last is not None and now - last < fresh:
                amount = kept.get(name)
            else:
                try:
                    amount = self._ask(source, lot)
                    answered[name] = now
                except Throttled as exc:
                    log.debug("price source %s held back for %s: %s", name, lot.token, exc)
                    complete = False
                    amount = kept.get(name)
            if amount is not None:
                answers.append((name, amount))
        quote = self.combine(lot, answers)
        with self._lock:
            self._cache[key] = quote
            self._answered[key] = answered
            if complete:
                self._asked[key] = now
        return quote

    def note(self, lot: LotView, source: str, amount: int) -> None:
        """An answer learnt elsewhere (the native rail's route quote for the
        whole lot): one more level, replacing that source's previous one."""
        key = self._key(lot.auction, lot.token)
        with self._lock:
            known = self._cache.get(key)
            if known is None:
                self._cache[key] = _quote([(source, amount)], self.clock(), lot.available)
            else:
                self._cache[key] = known.scaled(lot.available).with_level(
                    source, amount, self.clock()
                )

    def cached(self, auction: str, token: str, amount: int) -> MarketQuote | None:
        """The remembered estimate scaled to ``amount``; no source is asked."""
        with self._lock:
            quote = self._cache.get(self._key(auction, token))
        return quote.scaled(amount) if quote is not None else None

    def age(self, auction: str, token: str) -> float | None:
        """Seconds since the sources were last asked about the token (None: never)."""
        with self._lock:
            asked = self._asked.get(self._key(auction, token))
        return None if asked is None else self.clock() - asked

    # Persistence (the state file's ``prices`` section)

    def dump(self) -> dict[str, Any]:
        with self._lock:
            return {
                f"{auction}:{token}": {
                    "asked": self._asked.get((auction, token), 0.0),
                    "quote": asdict(quote) if quote is not None else None,
                }
                for (auction, token), quote in self._cache.items()
            }

    def load(self, raw: dict[str, Any]) -> None:
        with self._lock:
            for key, entry in raw.items():
                auction, _, token = key.partition(":")
                quote = entry.get("quote")
                if quote:
                    try:
                        quote = MarketQuote(
                            **{
                                **quote,
                                "levels": tuple(
                                    (str(name), int(want)) for name, want in quote.get("levels", ())
                                ),
                            }
                        )
                    except TypeError:
                        continue  # written by an older keeper: asked again
                self._cache[(auction, token)] = quote or None
                self._asked[(auction, token)] = float(entry.get("asked", 0.0))


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
