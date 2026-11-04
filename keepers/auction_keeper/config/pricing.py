"""The ``pricing`` and ``ttl`` sections: the market estimate behind the order
schedule and the validity bands around it (``keepers.auction_keeper.pricing``,
``keepers.auction_keeper.core.schedule``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from keepers.auction_keeper.config.common import ConfigError, fraction, positive_int

PRICE_SOURCES = ("curve", "llama", "coingecko", "cow", "kyber", "velora", "oneinch", "router")
DEFAULT_PRICE_SOURCES = ("curve", "llama", "cow")
DEFAULT_PRICE_CACHE = 60
DEFAULT_PRICE_CROSS_CHECK = 0.25


@dataclass(frozen=True)
class PricingSettings:
    """Market estimate sources in priority order (``keepers.auction_keeper.pricing``)."""

    sources: tuple[str, ...] = DEFAULT_PRICE_SOURCES
    cache: int = DEFAULT_PRICE_CACHE
    # Largest relative disagreement tolerated between the winning source and
    # the next one with a price; beyond it the lot has no market. 0 disables.
    cross_check: float = DEFAULT_PRICE_CROSS_CHECK
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

    near_above: float = 0.2
    near_below: float = 0.1
    min_ttl: int = 60
    max_ttl: int = 7200


def parse_pricing(raw: Any, name: str, default: PricingSettings) -> PricingSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(raw) - {
        "sources",
        "cache",
        "cross_check",
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
    cross_check = raw.get("cross_check", default.cross_check)
    if (
        isinstance(cross_check, bool)
        or not isinstance(cross_check, int | float)
        or not 0 <= cross_check < 1
    ):
        raise ConfigError(f"{name}.cross_check must be a fraction in [0, 1)")
    return PricingSettings(
        sources=sources,
        cache=positive_int(raw.get("cache"), f"{name}.cache", default.cache),
        cross_check=float(cross_check),
        chains=chains,
        coingecko_url=url,
        **key_envs,
    )


def parse_ttl(raw: Any, name: str, default: TtlSettings) -> TtlSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(raw) - {"near_above", "near_below", "min", "max"}
    if unknown:
        raise ConfigError(f"{name}: unknown setting(s) {sorted(unknown)}")

    settings = TtlSettings(
        near_above=fraction(raw.get("near_above"), f"{name}.near_above", default.near_above),
        near_below=fraction(raw.get("near_below"), f"{name}.near_below", default.near_below),
        min_ttl=positive_int(raw.get("min"), f"{name}.min", default.min_ttl),
        max_ttl=positive_int(raw.get("max"), f"{name}.max", default.max_ttl),
    )
    if settings.min_ttl > settings.max_ttl:
        raise ConfigError(f"{name}: min must not exceed max")
    return settings


__all__ = [
    "DEFAULT_PRICE_CACHE",
    "DEFAULT_PRICE_CROSS_CHECK",
    "DEFAULT_PRICE_SOURCES",
    "PRICE_SOURCES",
    "PricingSettings",
    "TtlSettings",
    "parse_pricing",
    "parse_ttl",
]
