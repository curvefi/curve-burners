"""The ``pricing`` and ``ttl`` sections: the market estimate behind the order
schedule and the validity bands around it (``keepers.auction_keeper.pricing``,
``keepers.auction_keeper.core.schedule``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from keepers.auction_keeper.config.common import (
    ConfigError,
    fraction,
    non_negative,
    positive_int,
)

PRICE_SOURCES = (
    "curve",
    "llama",
    "coingecko",
    "cow",
    "kyber",
    "velora",
    "oneinch",
    "router",
    "electric",
)
DEFAULT_PRICE_SOURCES = ("curve", "llama", "cow")
DEFAULT_PRICE_CACHE = 60
DEFAULT_PRICE_CACHE_FAR = 3600


@dataclass(frozen=True)
class PricingSettings:
    """Market estimate sources (``keepers.auction_keeper.pricing``): all are
    asked, each answer is a price level the schedule posts densely around."""

    sources: tuple[str, ...] = DEFAULT_PRICE_SOURCES
    # Seconds an estimate is reused for a lot near its market, and for one
    # far from it (never longer than half the time the curve needs to get near).
    cache: int = DEFAULT_PRICE_CACHE
    cache_far: int = DEFAULT_PRICE_CACHE_FAR
    # Seconds a cycle may spend refreshing estimates, and lots asked at once.
    budget: float = 5.0
    workers: int = 4
    # Chain slug per source (curve, llama, coingecko, kyber); defaults by chain id.
    chains: dict[str, str] = field(default_factory=dict)
    # CoinGecko: a demo key with the public API, or a pro key with the pro-api URL.
    coingecko_api_key_env: str | None = "COINGECKO_API_KEY"
    coingecko_url: str = "https://api.coingecko.com/api/v3"
    # The 1inch swap quote needs a Developer Portal key; without it the source is skipped.
    oneinch_api_key_env: str | None = "ONEINCH_API_KEY"

    @property
    def coingecko_api_key(self) -> str | None:
        return os.environ.get(self.coingecko_api_key_env) if self.coingecko_api_key_env else None

    @property
    def oneinch_api_key(self) -> str | None:
        return os.environ.get(self.oneinch_api_key_env) if self.oneinch_api_key_env else None


@dataclass(frozen=True)
class TtlSettings:
    """Order validity by distance from the market (``keepers.auction_keeper.core.schedule``)."""

    # The dense band around each source's price. The curve falls about a
    # third of a percent per step, so 5% over and 3% under a price is some
    # 25 steps (a quarter of an hour) of dense orders per price.
    near_above: float = 0.05
    near_below: float = 0.03
    min_ttl: int = 60
    max_ttl: int = 7200
    # Inside the band, replace an order as soon as the curve steps below its
    # limit instead of when it expires (two orders of a lot are then open at
    # once for up to `min`).
    every_step: bool = False


def parse_pricing(raw: Any, name: str, default: PricingSettings) -> PricingSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(raw) - {
        "sources",
        "cache",
        "cache_far",
        "budget",
        "workers",
        "chains",
        "coingecko_api_key_env",
        "coingecko_url",
        "oneinch_api_key_env",
    }
    if unknown:
        raise ConfigError(f"{name}: unknown setting(s) {sorted(unknown)}")
    sources: tuple[str, ...] = default.sources
    if "sources" in raw:
        if not isinstance(raw["sources"], list) or not all(
            isinstance(item, str) for item in raw["sources"]
        ):
            raise ConfigError(f"{name}.sources must be a list of source names")
        bad = [item for item in raw["sources"] if item not in PRICE_SOURCES]
        if bad:
            raise ConfigError(f"{name}.sources: unknown {bad}; choose from {list(PRICE_SOURCES)}")
        if len(set(raw["sources"])) != len(raw["sources"]):
            raise ConfigError(f"{name}.sources lists a source twice")
        sources = tuple(raw["sources"])
    chains = dict(default.chains)
    if "chains" in raw:
        if not isinstance(raw["chains"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw["chains"].items()
        ):
            raise ConfigError(f"{name}.chains must map source names to chain slugs")
        bad = [k for k in raw["chains"] if k not in PRICE_SOURCES]
        if bad:
            raise ConfigError(f"{name}.chains: unknown source(s) {bad}")
        chains.update(raw["chains"])
    key_envs = {}
    for key in ("coingecko_api_key_env", "oneinch_api_key_env"):
        key_env = raw.get(key, getattr(default, key))
        if key_env is not None and not isinstance(key_env, str):
            raise ConfigError(f"{name}.{key} must be a string")
        key_envs[key] = key_env
    url = raw.get("coingecko_url", default.coingecko_url)
    if not isinstance(url, str) or not url:
        raise ConfigError(f"{name}.coingecko_url must be a URL")
    cache = positive_int(raw.get("cache"), f"{name}.cache", default.cache)
    cache_far = positive_int(raw.get("cache_far"), f"{name}.cache_far", default.cache_far)
    return PricingSettings(
        sources=sources,
        cache=cache,
        cache_far=max(cache, cache_far),
        budget=non_negative(raw.get("budget"), f"{name}.budget", default.budget),
        workers=positive_int(raw.get("workers"), f"{name}.workers", default.workers),
        chains=chains,
        coingecko_url=url,
        **key_envs,
    )


def parse_ttl(raw: Any, name: str, default: TtlSettings) -> TtlSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(raw) - {"near_above", "near_below", "min", "max", "every_step"}
    if unknown:
        raise ConfigError(f"{name}: unknown setting(s) {sorted(unknown)}")
    every_step = raw.get("every_step", default.every_step)
    if not isinstance(every_step, bool):
        raise ConfigError(f"{name}.every_step must be a boolean")
    settings = TtlSettings(
        near_above=fraction(raw.get("near_above"), f"{name}.near_above", default.near_above),
        near_below=fraction(raw.get("near_below"), f"{name}.near_below", default.near_below),
        min_ttl=positive_int(raw.get("min"), f"{name}.min", default.min_ttl),
        max_ttl=positive_int(raw.get("max"), f"{name}.max", default.max_ttl),
        every_step=every_step,
    )
    if settings.min_ttl > settings.max_ttl:
        raise ConfigError(f"{name}: min must not exceed max")
    return settings


__all__ = [
    "DEFAULT_PRICE_CACHE",
    "DEFAULT_PRICE_CACHE_FAR",
    "DEFAULT_PRICE_SOURCES",
    "PRICE_SOURCES",
    "PricingSettings",
    "TtlSettings",
    "parse_pricing",
    "parse_ttl",
]
