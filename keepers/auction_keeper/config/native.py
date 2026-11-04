"""The ``native`` section: the keeper's own rail through the AuctionTaker
with a Curve router swap (``keepers.auction_keeper.rails.native``)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from keepers.auction_keeper.config.common import (
    ConfigError,
    address,
    fraction,
    non_empty_string,
    positive_int,
)


@dataclass(frozen=True)
class NativeSettings:
    """The native rail: takes through the AuctionTaker with a Curve router
    swap (``keepers.auction_keeper.rails.native``). Needs ``auction_taker`` and, to
    send, ``mode: keeper``; offline mode simulates and reports."""

    enabled: bool = False
    # Curve router NG; defaults by chain id (routing.curve_router.ROUTERS).
    router: str | None = None
    # Take when the auction's quote is at least this far under the route quote.
    margin: float = 0.005
    # Chunk the take so the route's price impact stays under this fraction.
    max_impact: float = 0.01
    # Seconds between polls while a lot's quote is within the band above the route.
    poll: int = 30
    # Pools below this TVL (USD) are not routed through; hops per route.
    min_pool_usd: float = 1000.0
    max_hops: int = 2
    # api.curve.finance pool list: base URL and chain slug (defaults by chain id).
    api_url: str = "https://api.curve.finance/v1/getPools/all"
    chain: str | None = None
    # Receiver of the take's profit (the sender by default).
    profit_receiver: str | None = None
    # Explicit routes by lot token, used instead of the search:
    # {token: {path: [token, pool, token, ...], params: [[i, j, swap, pool_type, n], ...]}}
    routes: dict[str, Any] = field(default_factory=dict)


def parse_native(raw: Any, name: str, default: NativeSettings) -> NativeSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    allowed = {f.name for f in fields(NativeSettings)}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{name}: unknown setting(s) {sorted(unknown)}")
    enabled = raw.get("enabled", default.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{name}.enabled must be a boolean")
    router = raw.get("router", default.router)
    if router is not None:
        router = address(router, f"{name}.router")
    receiver = raw.get("profit_receiver", default.profit_receiver)
    if receiver is not None:
        receiver = address(receiver, f"{name}.profit_receiver")
    min_pool_usd = raw.get("min_pool_usd", default.min_pool_usd)
    if (
        isinstance(min_pool_usd, bool)
        or not isinstance(min_pool_usd, int | float)
        or min_pool_usd < 0
    ):
        raise ConfigError(f"{name}.min_pool_usd must be a non-negative number")
    routes = dict(default.routes)
    if "routes" in raw:
        from keepers.auction_keeper.routing.curve_router import route_from_config

        if not isinstance(raw["routes"], dict):
            raise ConfigError(f"{name}.routes must map tokens to routes")
        for token, route in raw["routes"].items():
            token = address(token, f"{name}.routes")
            try:
                routes[token] = route_from_config(route)
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigError(f"{name}.routes[{token}]: {exc}") from exc
    return NativeSettings(
        enabled=enabled,
        router=router,
        margin=fraction(raw.get("margin"), f"{name}.margin", default.margin),
        max_impact=fraction(raw.get("max_impact"), f"{name}.max_impact", default.max_impact),
        poll=positive_int(raw.get("poll"), f"{name}.poll", default.poll),
        min_pool_usd=float(min_pool_usd),
        max_hops=positive_int(raw.get("max_hops"), f"{name}.max_hops", default.max_hops),
        api_url=non_empty_string(raw.get("api_url"), f"{name}.api_url", default.api_url) or "",
        chain=non_empty_string(raw.get("chain"), f"{name}.chain", default.chain),
        profit_receiver=receiver,
        routes=routes,
    )


__all__ = ["NativeSettings", "parse_native"]
