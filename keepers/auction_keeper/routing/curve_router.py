"""Curve router NG: routes from the Curve pool catalogue, quotes on-chain.

The router contract takes a path of up to five hops (``token, pool, token,
pool, ...``) with per-hop swap parameters; it does no route finding itself.
Routes are found here from ``api.curve.finance`` pool data (coins, pool
kind, TVL), limited to plain ``exchange`` hops on pools above a TVL floor,
and ranked by the router's own ``get_dy`` so the quote the keeper acts on is
exactly what the swap would pay.

Pool kinds map to the router's ``pool_type``: 1 stable (int128 indices),
2 twocrypto, 3 tricrypto (uint256 indices). Metapool underlying swaps, zaps,
lending and LLAMMA hops are not routed; an explicit route in the config
(``native.routes``) covers anything the search does not.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.abi import CallReverted, selector
from keepers.auction_keeper.chain.client import ChainClient
from keepers.auction_keeper.execute import Call, curve_router_exchange
from keepers.auction_keeper.rails.common import JsonClient

log = logging.getLogger(__name__)

# Curve router NG by chain id (the deployment answering get_dy/exchange with
# the address[11] / uint256[5][5] ABI); override with native.router.
ROUTERS: dict[int, str] = {
    1: "0x45312ea0eff7e09c83cbe249fa1d7598c4c8cd4e",
    10: "0xF0d4c12A5768D806021F80a262B4d39d26C58b8D",
    100: "0xF0d4c12A5768D806021F80a262B4d39d26C58b8D",
    137: "0xF0d4c12A5768D806021F80a262B4d39d26C58b8D",
    250: "0xF0d4c12A5768D806021F80a262B4d39d26C58b8D",
    252: "0x4f37A9d177470499A2dD084621020b023fcffc1F",
    8453: "0x4f37A9d177470499A2dD084621020b023fcffc1F",
    42161: "0x2191718CD32d02B8E60BAdFFeA33E4B5DD9A0A0D",
    43114: "0xF0d4c12A5768D806021F80a262B4d39d26C58b8D",
}
# api.curve.finance chain slugs by chain id; override with native.chain.
API_CHAINS: dict[int, str] = {
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
}
DEFAULT_API_URL = "https://api.curve.finance/v1/getPools/all"

ZERO = "0x" + "00" * 20
SWAP_EXCHANGE = 1
GET_DY = selector("get_dy(address[11],uint256[5][5],uint256,address[5])")


@dataclass(frozen=True)
class Pool:
    address: str
    coins: tuple[str, ...]  # lowercase
    pool_type: int  # 1 stable, 2 twocrypto, 3 tricrypto
    usd_total: float
    name: str = ""


@dataclass(frozen=True)
class Route:
    """A router path: ``path`` = token, pool, token, ...; one swap-params row per hop."""

    path: tuple[str, ...]
    swap_params: tuple[tuple[int, int, int, int, int], ...]

    @property
    def hops(self) -> int:
        return len(self.swap_params)

    @property
    def pools(self) -> tuple[str, ...]:
        return self.path[1::2]

    def __str__(self) -> str:
        return " -> ".join(self.path)


def pool_type(entry: dict[str, Any]) -> int | None:
    registry = entry.get("registryId") or ""
    n_coins = len(entry.get("coins") or [])
    if registry in ("factory-stable-ng", "factory", "main"):
        return 1
    if registry in ("factory-twocrypto", "factory-crypto"):
        return 2
    if registry == "factory-tricrypto":
        return 3
    if registry == "crypto":
        return 3 if n_coins == 3 else 2
    return None


def parse_pools(payload: dict[str, Any], *, min_usd: float = 0.0) -> list[Pool]:
    """Pools from an ``api.curve.finance/v1/getPools/all/{chain}`` answer that
    the router can trade through: a known kind, not broken, above ``min_usd``."""
    pools: list[Pool] = []
    for entry in ((payload or {}).get("data") or {}).get("poolData") or []:
        kind = pool_type(entry)
        coins = tuple(str(coin.get("address", "")).lower() for coin in entry.get("coins") or [])
        if kind is None or len(coins) < 2 or entry.get("isBroken"):
            continue
        usd_total = float(entry.get("usdTotal") or 0.0)
        if usd_total < min_usd:
            continue
        pools.append(
            Pool(
                address=to_checksum_address(entry["address"]),
                coins=coins,
                pool_type=kind,
                usd_total=usd_total,
                name=str(entry.get("name") or ""),
            )
        )
    return pools


class PoolGraph:
    """Pools indexed by coin, with route search up to ``max_hops`` plain exchanges."""

    def __init__(self, pools: Iterable[Pool]):
        self.pools = list(pools)
        self.by_coin: dict[str, list[Pool]] = defaultdict(list)
        for pool in self.pools:
            for coin in pool.coins:
                self.by_coin[coin].append(pool)

    @staticmethod
    def _hop(pool: Pool, token_in: str, token_out: str) -> tuple[int, int, int, int, int]:
        i, j = pool.coins.index(token_in), pool.coins.index(token_out)
        return (i, j, SWAP_EXCHANGE, pool.pool_type, len(pool.coins))

    def routes(self, token: str, want: str, *, max_hops: int = 2) -> list[Route]:
        """Candidate routes, deepest pools first (by the shallowest pool on the route)."""
        token, want = token.lower(), want.lower()
        if token == want:
            return []
        found: dict[tuple[str, ...], tuple[Route, float]] = {}
        for pool in self.by_coin.get(token, []):
            if want in pool.coins:
                route = Route((token, pool.address, want), (self._hop(pool, token, want),))
                found[route.path] = (route, pool.usd_total)
        if max_hops >= 2:
            for first in self.by_coin.get(token, []):
                for mid in first.coins:
                    if mid in (token, want):
                        continue
                    for second in self.by_coin.get(mid, []):
                        if second.address == first.address or want not in second.coins:
                            continue
                        route = Route(
                            (token, first.address, mid, second.address, want),
                            (self._hop(first, token, mid), self._hop(second, mid, want)),
                        )
                        found[route.path] = (route, min(first.usd_total, second.usd_total))
        ranked = sorted(found.values(), key=lambda item: (-item[1], item[0].hops))
        return [route for route, _ in ranked]


class PoolCatalog:
    """The chain's pool list from the Curve API, refreshed every ``ttl`` seconds."""

    def __init__(
        self,
        chain: str,
        *,
        http: Any | None = None,
        api_url: str = DEFAULT_API_URL,
        min_usd: float = 0.0,
        ttl: int = 600,
        clock: Callable[[], float] = time.time,
    ):
        self.chain = chain
        self.http = http or JsonClient(timeout=60.0)
        self.api_url = api_url.rstrip("/")
        self.min_usd = min_usd
        self.ttl = ttl
        self.clock = clock
        self._graph: PoolGraph | None = None
        self._loaded_at = 0.0

    def graph(self) -> PoolGraph:
        now = self.clock()
        if self._graph is None or now - self._loaded_at >= self.ttl:
            try:
                payload = self.http.request("GET", f"{self.api_url}/{self.chain}")
            except Exception:
                if self._graph is None:
                    raise
                log.warning("curve pool catalogue refresh failed; keeping the previous one")
                return self._graph
            self._graph = PoolGraph(parse_pools(payload, min_usd=self.min_usd))
            self._loaded_at = now
            log.debug("curve catalogue %s: %s routable pools", self.chain, len(self._graph.pools))
        return self._graph


class CurveRouter:
    """Quotes and swap calls through the Curve router NG for the best route."""

    def __init__(
        self,
        client: ChainClient,
        address: str,
        graph: Callable[[], PoolGraph],
        *,
        max_hops: int = 2,
        max_candidates: int = 6,
        routes: dict[str, Route] | None = None,
    ):
        self.client = client
        self.address = to_checksum_address(address)
        self.graph = graph
        self.max_hops = max_hops
        self.max_candidates = max_candidates
        # Explicit routes by lowercase lot token: used instead of the search.
        self.routes = {token.lower(): route for token, route in (routes or {}).items()}

    def get_dy(self, route: Route, amount: int) -> int:
        path = [to_checksum_address(a) for a in route.path] + [ZERO] * (11 - len(route.path))
        params = [list(p) for p in route.swap_params] + [[0] * 5] * (5 - route.hops)
        data = GET_DY + encode(
            ["address[11]", "uint256[5][5]", "uint256", "address[5]"],
            [path, params, amount, [ZERO] * 5],
        )
        try:
            return int(decode(["uint256"], self.client.call(self.address, data))[0])
        except CallReverted as exc:
            log.debug("router get_dy reverted for %s: %s", route, exc)
            return 0

    def candidates(self, token: str, want: str) -> list[Route]:
        explicit = self.routes.get(token.lower())
        if explicit is not None:
            return [explicit]
        return self.graph().routes(token, want, max_hops=self.max_hops)[: self.max_candidates]

    def best_route(self, token: str, want: str, amount: int) -> tuple[Route, int] | None:
        """The candidate paying the most want for ``amount``; None when nothing routes."""
        best: tuple[Route, int] | None = None
        for route in self.candidates(token, want):
            dy = self.get_dy(route, amount)
            if dy > 0 and (best is None or dy > best[1]):
                best = (route, dy)
        return best

    def exchange_call(self, route: Route, amount: int, min_dy: int, receiver: str) -> Call:
        return curve_router_exchange(
            self.address,
            list(route.path),
            [list(p) for p in route.swap_params],
            amount,
            min_dy,
            receiver,
        )


def curve_router_for(config: Any, client: ChainClient, *, http: Any | None = None) -> CurveRouter:
    """A ``CurveRouter`` from a network config's ``native`` section (the router
    address and pool catalogue default by chain id)."""
    from keepers.auction_keeper.config import ConfigError

    settings = config.native
    address = settings.router or ROUTERS.get(client.chain_id)
    if address is None:
        raise ConfigError(
            f"{config.name}: no Curve router known for chain {client.chain_id}; set native.router"
        )
    chain = settings.chain or API_CHAINS.get(client.chain_id)
    if chain is None:
        raise ConfigError(
            f"{config.name}: no Curve API slug for chain {client.chain_id}; set native.chain"
        )
    catalog = PoolCatalog(chain, http=http, api_url=settings.api_url, min_usd=settings.min_pool_usd)
    return CurveRouter(
        client, address, catalog.graph, max_hops=settings.max_hops, routes=settings.routes
    )


def route_from_config(raw: Sequence[Sequence[Any]] | dict[str, Any]) -> Route:
    """``{path: [token, pool, token, ...], params: [[i, j, swap, pool_type, n], ...]}``."""
    path = tuple(to_checksum_address(a) for a in raw["path"])
    params = tuple(tuple(int(x) for x in row) for row in raw["params"])
    if len(path) < 3 or len(path) % 2 == 0 or len(params) != len(path) // 2:
        raise ValueError("route path must be token, pool, token, ... with one params row per hop")
    if any(len(row) != 5 for row in params):
        raise ValueError("each swap params row has five entries")
    return Route(path, params)  # type: ignore[arg-type]


__all__ = [
    "API_CHAINS",
    "DEFAULT_API_URL",
    "ROUTERS",
    "CurveRouter",
    "Pool",
    "PoolCatalog",
    "PoolGraph",
    "Route",
    "curve_router_for",
    "parse_pools",
    "pool_type",
    "route_from_config",
]
