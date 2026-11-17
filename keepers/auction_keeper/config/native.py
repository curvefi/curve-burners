"""The ``native`` section: the keeper's own rail through the AuctionTaker
with a router swap (``keepers.auction_keeper.rails.native``)."""

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

# Routers the native rail can swap through; each is one entry of
# rails.native.BACKENDS.
NATIVE_BACKENDS = ("curve", "electric")


@dataclass(frozen=True)
class ElectricSettings:
    """The electric router backend (``keepers.auction_keeper.routing.electric_router``): the
    ``erouter`` command line of michwill/electric-router, run per quote."""

    # The command, e.g. [/path/to/venv/bin/erouter]; found on PATH by default.
    command: tuple[str, ...] = ("erouter",)
    # erouter chain name; defaults by chain id (electric_router.CHAINS).
    chain: str | None = None
    # RPC endpoint for the tool; its own committed endpoint by default.
    rpc_url: str | None = None
    # Refuse a call to any other ElectricRouter address (unset: trust the tool).
    router: str | None = None
    # Whole-route tolerance in bp instead of the tool's per-pool rule.
    slippage_bp: float | None = None
    # Legs per route: the AuctionTaker takes 1888 bytes of calldata per step.
    max_legs: int = 16
    # Pool TVL floor in USD (the tool's default is 10000).
    min_tvl: float | None = None
    # Seconds one erouter run may take, and how long its answer is reused.
    timeout: int = 180
    cache: int = 30


@dataclass(frozen=True)
class NativeSettings:
    """The native rail: takes through the AuctionTaker with a router swap
    (``keepers.auction_keeper.rails.native``). Needs ``auction_taker`` and, to
    send, ``mode: keeper``; offline mode simulates and reports."""

    enabled: bool = False
    # The routers, any of NATIVE_BACKENDS: curve (router NG over catalogue
    # routes), electric. With several, each lot is quoted on all of them and
    # swapped through the one that pays more.
    backends: tuple[str, ...] = ("curve",)
    # Lots quoted at once (threads; the takes themselves go one by one).
    workers: int = 4
    electric: ElectricSettings = ElectricSettings()
    # Curve router NG; defaults by chain id (routing.curve_router.ROUTERS).
    router: str | None = None
    # Take when the auction's quote is at least this far under the route quote.
    margin: float = 0.005
    # Chunk the take so the route's price impact stays under this fraction.
    max_impact: float = 0.01
    # Seconds between polls while a lot's quote is within the band above the route.
    poll: int = 30
    # Pools below this TVL (USD) are not routed through; hops per route (the
    # router takes five at most).
    min_pool_usd: float = 1000.0
    max_hops: int = 5
    # api.curve.finance pool list: base URL and chain slug (defaults by chain id).
    api_url: str = "https://api.curve.finance/v1/getPools/all"
    chain: str | None = None
    # Receiver of the take's profit (the sender by default).
    profit_receiver: str | None = None
    # Explicit routes by lot token, used instead of the search:
    # {token: {path: [token, pool, token, ...], params: [[i, j, swap, pool_type, n], ...],
    #          pools: [pool or zero per hop]}}  (pools only for zap hops, swap type 3)
    routes: dict[str, Any] = field(default_factory=dict)


def _number(value: Any, name: str, default: float | None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ConfigError(f"{name} must be a non-negative number")
    return float(value)


def parse_electric(raw: Any, name: str, default: ElectricSettings) -> ElectricSettings:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    unknown = set(raw) - {f.name for f in fields(ElectricSettings)}
    if unknown:
        raise ConfigError(f"{name}: unknown setting(s) {sorted(unknown)}")
    command = raw.get("command", default.command)
    if isinstance(command, str):
        command = [command]
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ConfigError(f"{name}.command must be a command or a list of its words")
    router = raw.get("router", default.router)
    if router is not None:
        router = address(router, f"{name}.router")
    return ElectricSettings(
        command=tuple(command),
        chain=non_empty_string(raw.get("chain"), f"{name}.chain", default.chain),
        rpc_url=non_empty_string(raw.get("rpc_url"), f"{name}.rpc_url", default.rpc_url),
        router=router,
        slippage_bp=_number(raw.get("slippage_bp"), f"{name}.slippage_bp", default.slippage_bp),
        max_legs=positive_int(raw.get("max_legs"), f"{name}.max_legs", default.max_legs),
        min_tvl=_number(raw.get("min_tvl"), f"{name}.min_tvl", default.min_tvl),
        timeout=positive_int(raw.get("timeout"), f"{name}.timeout", default.timeout),
        cache=positive_int(raw.get("cache"), f"{name}.cache", default.cache),
    )


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
    backends = raw.get("backends", list(default.backends))
    if (
        not isinstance(backends, list)
        or not backends
        or len(set(backends)) != len(backends)
        or any(backend not in NATIVE_BACKENDS for backend in backends)
    ):
        raise ConfigError(
            f"{name}.backends must be a non-empty list out of {list(NATIVE_BACKENDS)}"
        )
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
        backends=tuple(backends),
        workers=positive_int(raw.get("workers"), f"{name}.workers", default.workers),
        electric=parse_electric(raw.get("electric"), f"{name}.electric", default.electric),
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


__all__ = ["NATIVE_BACKENDS", "ElectricSettings", "NativeSettings", "parse_native"]
