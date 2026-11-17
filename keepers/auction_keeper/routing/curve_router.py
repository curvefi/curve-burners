"""Curve router NG: routes from the Curve pool catalogue, quotes on-chain.

The router contract takes a path of up to five hops (``token, pool, token,
pool, ...``) with per-hop swap parameters; it does no route finding itself.
Routes are found here from Curve's own data and ranked by the router's
``get_dy`` so the quote the keeper acts on is what the swap would pay. Every
operation of the router is a hop kind (its ``swap_type``):

* 1 ``exchange`` between two coins of a pool (native ETH coins included),
  and through a LLAMMA (crvUSD and lending markets, ``pool_type`` 4);
* 2 ``exchange_underlying``: metapools (meta coin <-> base pool coins) and
  lending pools (between the underlying coins of their aTokens);
* 3 underlying exchange through a zap: metapools over a lending base pool
  and crypto metapools (the zap goes in the path, the pool in ``_pools``);
* 4 coin -> LP token (``add_liquidity``) and 6 LP token -> coin
  (``remove_liquidity_one_coin``), so LP lots unwrap and a path can cross a
  base pool;
* 5 lending underlying coin -> LP token and 7 LP token -> lending underlying;
* 8 wrappers: ETH <-> wrapped native everywhere; on Ethereum also
  ETH -> stETH, ETH -> frxETH, stETH <-> wstETH, ETH -> wBETH;
* 9 ERC4626 asset <-> share, where the deployed router has it.

Pool kinds map to the router's ``pool_type``: 1 stable (int128 indices),
2 twocrypto, 3 tricrypto (uint256 indices), 4 LLAMMA; the liquidity hops also
tell the NG pools apart (10 stable-ng, 20 twocrypto-ng, 30 tricrypto-ng),
whose ``add_liquidity`` ABI differs.

Sources: ``api.curve.finance`` pools (coins, LP token, kind, metapool base
and zap, TVL) and lending vaults (LLAMMA and ERC4626 vault per market),
``prices.curve.finance`` crvUSD markets, and the chain for what no API
says (lending pools' underlying coins, ERC4626 coins, the router's wrapped
native token). Hops are generated generously: one the deployed router or
pool does not take reverts in ``get_dy`` and the route is dropped. The quote
is approximate for the liquidity hops; the take is simulated before it is
sent either way. An explicit route in the config (``native.routes``)
replaces the search for a token.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
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
    10: "0x0DCDED3545D565bA3B19E683431381007245d983",
    56: "0xA72C85C258A81761433B4e8da60505Fe3Dd551CC",
    100: "0x0DCDED3545D565bA3B19E683431381007245d983",
    137: "0x0DCDED3545D565bA3B19E683431381007245d983",
    196: "0xBFab8ebc836E1c4D81837798FC076D219C9a1855",
    250: "0x0DCDED3545D565bA3B19E683431381007245d983",
    # Fraxtal: the curve-router-ng README lists 0xC589...A873; this one is
    # the deployment the keeper has been taking through.
    252: "0x4f37A9d177470499A2dD084621020b023fcffc1F",
    5000: "0x4f37A9d177470499A2dD084621020b023fcffc1F",
    8453: "0x4f37A9d177470499A2dD084621020b023fcffc1F",
    42161: "0x2191718CD32d02B8E60BAdFFeA33E4B5DD9A0A0D",
    43114: "0x0DCDED3545D565bA3B19E683431381007245d983",
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
LENDING_API_URL = "https://api.curve.finance/v1/getLendingVaults/all"
CRVUSD_API_URL = "https://prices.curve.finance/v1/crvusd/markets"

ZERO = "0x" + "00" * 20
SWAP_EXCHANGE = 1
SWAP_EXCHANGE_UNDERLYING = 2
SWAP_ZAP = 3
SWAP_ADD_LIQUIDITY = 4
SWAP_ADD_LIQUIDITY_UNDERLYING = 5
SWAP_REMOVE_LIQUIDITY = 6
SWAP_REMOVE_LIQUIDITY_UNDERLYING = 7
SWAP_WRAPPER = 8
SWAP_ERC4626 = 9
POOL_LLAMMA = 4
MAX_ROUTER_HOPS = 5
ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
# The router's WETH_ADDRESS is not readable: candidates by chain id, each
# confirmed with a wrapper get_dy before its hops are offered.
WRAPPED_NATIVE: dict[int, tuple[str, ...]] = {
    1: ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",),
    10: ("0x4200000000000000000000000000000000000006",),
    56: ("0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",),
    100: ("0xe91d153e0b41518a2ce8dd3d7944fa863463a97d",),
    137: ("0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",),
    250: ("0x21be370d5312f44cb42ce377bc9b8a0cef1a4c83",),
    252: (
        "0xfc00000000000000000000000000000000000006",
        "0xfc00000000000000000000000000000000000002",
    ),
    5000: ("0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8",),
    8453: ("0x4200000000000000000000000000000000000006",),
    42161: ("0x82af49447d8a07e3bd95bd0d56f35241523fbab1",),
    43114: ("0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",),
}
# Ethereum-only wrapper hops of swap type 8: (token in, token out, the
# contract in the path).
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
WSTETH = "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
FRXETH = "0x5e8422345238f34275888049021821e8e08caa1f"
FRXETH_MINTER = "0xbafa44efe7901e04e39dad13167d089c559c1138"
WBETH = "0xa2e3356610840701bdf5611a53974510ae27e2e1"
ETHEREUM_WRAPPERS: tuple[tuple[str, str, str], ...] = (
    (ETH, STETH, STETH),
    (ETH, FRXETH, FRXETH_MINTER),
    (STETH, WSTETH, WSTETH),
    (WSTETH, STETH, WSTETH),
    (ETH, WBETH, WBETH),
)
ERC4626_ASSET = selector("asset()")
UNDERLYING_COINS = selector("underlying_coins(uint256)")
# registryId -> (pool_type for exchange, pool_type for the liquidity hops);
# the legacy "crypto" registry goes by the coin count.
POOL_TYPES: dict[str, tuple[int, int]] = {
    "main": (1, 1),
    "factory": (1, 1),
    "factory-stable-ng": (1, 10),
    "factory-crypto": (2, 2),
    "factory-twocrypto": (2, 20),
    "factory-tricrypto": (3, 30),
}
# Registries whose pools may be lending pools (aTokens over underlying coins).
LENDING_REGISTRIES = ("main",)
# Route search bound: paths explored per (token, want) before ranking.
MAX_PATHS = 20000
GET_DY = selector("get_dy(address[11],uint256[5][5],uint256,address[5])")
# The small amount, as a fraction of the lot, a route is quoted for when the
# whole lot pays nothing on any candidate.
PROBE_FRACTION = 1000


@dataclass(frozen=True)
class Pool:
    address: str
    coins: tuple[str, ...]  # lowercase
    pool_type: int  # 1 stable, 2 twocrypto, 3 tricrypto
    usd_total: float
    name: str = ""
    lp_token: str | None = None  # lowercase; the pool itself for factory pools
    liquidity_type: int | None = None  # pool_type for add/remove liquidity hops
    registry: str = ""
    underlying: tuple[str, ...] = ()  # lending pools: underlying coin per aToken
    # Metapools: the API's underlying coin list (meta coins, then the base
    # pool's), the base pool and the zap for underlying exchanges.
    meta_underlying: tuple[str, ...] = ()
    base_pool: str | None = None  # lowercase
    zap: str | None = None


@dataclass(frozen=True)
class Route:
    """A router path: ``path`` = token, pool, token, ...; one swap-params row per hop."""

    path: tuple[str, ...]
    swap_params: tuple[tuple[int, int, int, int, int], ...]
    # The router's ``_pools``: per hop, the pool behind a zap (swap type 3).
    zap_pools: tuple[str, ...] = ()

    @property
    def hops(self) -> int:
        return len(self.swap_params)

    @property
    def pools(self) -> tuple[str, ...]:
        return self.path[1::2]

    def __str__(self) -> str:
        return " -> ".join(self.path)


def pool_types(entry: dict[str, Any]) -> tuple[int, int] | None:
    """(pool_type for exchange, pool_type for liquidity hops) of an API pool entry."""
    registry = entry.get("registryId") or ""
    if registry == "crypto":
        kind = 3 if len(entry.get("coins") or []) == 3 else 2
        return kind, kind
    return POOL_TYPES.get(registry)


def pool_type(entry: dict[str, Any]) -> int | None:
    kinds = pool_types(entry)
    return kinds[0] if kinds else None


def parse_pools(payload: dict[str, Any], *, min_usd: float = 0.0) -> list[Pool]:
    """Pools from an ``api.curve.finance/v1/getPools/all/{chain}`` answer that
    the router can trade through: a known kind, not broken, above ``min_usd``."""
    pools: list[Pool] = []
    for entry in ((payload or {}).get("data") or {}).get("poolData") or []:
        kinds = pool_types(entry)
        coins = tuple(str(coin.get("address", "")).lower() for coin in entry.get("coins") or [])
        if kinds is None or len(coins) < 2 or entry.get("isBroken"):
            continue
        usd_total = float(entry.get("usdTotal") or 0.0)
        if usd_total < min_usd:
            continue
        pools.append(
            Pool(
                address=to_checksum_address(entry["address"]),
                coins=coins,
                pool_type=kinds[0],
                usd_total=usd_total,
                name=str(entry.get("name") or ""),
                lp_token=str(entry.get("lpTokenAddress") or "").lower() or None,
                liquidity_type=kinds[1],
                registry=str(entry.get("registryId") or ""),
                meta_underlying=tuple(
                    str(coin.get("address", "")).lower()
                    for coin in (entry.get("underlyingCoins") or [])
                )
                if entry.get("isMetaPool")
                else (),
                base_pool=str(entry.get("basePoolAddress") or "").lower() or None,
                zap=str(entry.get("zapAddress") or "").lower() or None,
            )
        )
    return pools


@dataclass(frozen=True)
class Hop:
    pool: Pool
    token_in: str
    token_out: str
    params: tuple[int, int, int, int, int]
    swap: str | None = None  # the address in the path when it is not the pool (a zap)
    zap_pool: str = ZERO  # the router's ``_pools`` entry (swap type 3)


def pool_hops(pool: Pool) -> list[Hop]:
    """Every router hop the pool offers (see the module docstring)."""
    n = len(pool.coins)
    hops = [
        Hop(pool, a, b, (i, j, SWAP_EXCHANGE, pool.pool_type, n))
        for i, a in enumerate(pool.coins)
        for j, b in enumerate(pool.coins)
        if i != j
    ]
    for i, coin in enumerate(pool.meta_underlying):
        for j, other in enumerate(pool.meta_underlying):
            if i != j and coin != other:
                params = (i, j, SWAP_EXCHANGE_UNDERLYING, pool.pool_type, n)
                hops.append(Hop(pool, coin, other, params))
    lp, kind = pool.lp_token, pool.liquidity_type
    if lp is None or kind is None or lp in pool.coins:
        return hops
    for i, coin in enumerate(pool.coins):
        hops.append(Hop(pool, lp, coin, (0, i, SWAP_REMOVE_LIQUIDITY, kind, n)))
        if n <= 5 or kind == 10:  # the router's fixed-size add_liquidity arrays
            hops.append(Hop(pool, coin, lp, (i, 0, SWAP_ADD_LIQUIDITY, kind, n)))
    if len(pool.underlying) == n:
        for i, coin in enumerate(pool.underlying):
            hops.append(Hop(pool, lp, coin, (0, i, SWAP_REMOVE_LIQUIDITY_UNDERLYING, kind, n)))
            if n == 3:  # the router's lending add_liquidity is three-coin only
                hops.append(Hop(pool, coin, lp, (i, 0, SWAP_ADD_LIQUIDITY_UNDERLYING, kind, n)))
            for j, other in enumerate(pool.underlying):
                if i != j:
                    params = (i, j, SWAP_EXCHANGE_UNDERLYING, pool.pool_type, n)
                    hops.append(Hop(pool, coin, other, params))
    return hops


def zap_hops(pool: Pool, by_address: dict[str, Pool]) -> list[Hop]:
    """Underlying exchanges of a metapool through its zap (swap type 3). Over
    a lending base pool the zap trades the aTokens' underlying coins, which
    the API does not list: both readings of the coin list are offered."""
    if not pool.zap or not pool.meta_underlying:
        return []
    lists = [pool.meta_underlying]
    base = by_address.get(pool.base_pool or "")
    if base is not None and base.underlying:
        unwrap = dict(zip(base.coins, base.underlying, strict=True))
        mapped = tuple(unwrap.get(coin, coin) for coin in pool.meta_underlying)
        if mapped != pool.meta_underlying:
            lists.append(mapped)
    zap = to_checksum_address(pool.zap)
    hops = []
    for coins in lists:
        for i, coin in enumerate(coins):
            for j, other in enumerate(coins):
                if i != j and coin != other:
                    params = (i, j, SWAP_ZAP, pool.pool_type, len(pool.coins))
                    hops.append(Hop(pool, coin, other, params, swap=zap, zap_pool=pool.address))
    return hops


def pair_pool(
    address: str, token_in: str, token_out: str, kind: int, usd: float, name: str
) -> Pool:
    """The stand-in pool of a hop that is not a catalogue pool (a wrapper, a
    vault, a LLAMMA): what the search needs to rank it and visit it once."""
    return Pool(
        address=to_checksum_address(address),
        coins=(token_in.lower(), token_out.lower()),
        pool_type=kind,
        usd_total=usd,
        name=name,
    )


def wrapper_hops(pairs: Iterable[tuple[str, str, str]]) -> list[Hop]:
    """Swap type 8 hops from (token in, token out, contract) triples."""
    hops = []
    pools: dict[tuple[str, frozenset[str]], Pool] = {}
    for token_in, token_out, contract in pairs:
        key = (contract.lower(), frozenset((token_in.lower(), token_out.lower())))
        pool = pools.setdefault(
            key, pair_pool(contract, token_in, token_out, 0, float("inf"), "wrapper")
        )
        hops.append(Hop(pool, token_in.lower(), token_out.lower(), (0, 0, SWAP_WRAPPER, 0, 0)))
    return hops


def erc4626_hops(vaults: Iterable[tuple[str, str]]) -> list[Hop]:
    """Swap type 9 hops from (vault, asset) pairs: deposit and redeem."""
    hops = []
    for vault, asset in vaults:
        pool = pair_pool(vault, asset, vault, 0, float("inf"), "erc4626")
        hops.append(Hop(pool, asset.lower(), vault.lower(), (0, 0, SWAP_ERC4626, 0, 0)))
        hops.append(Hop(pool, vault.lower(), asset.lower(), (1, 0, SWAP_ERC4626, 0, 0)))
    return hops


def llamma_hops(amms: Iterable[tuple[str, str, str, float]]) -> list[Hop]:
    """Exchanges through LLAMMAs from (amm, borrowed, collateral, usd): coin 0
    is the borrowed token, coin 1 the collateral."""
    hops = []
    for amm, borrowed, collateral, usd in amms:
        pool = pair_pool(amm, borrowed, collateral, POOL_LLAMMA, usd, "llamma")
        for i, j in ((0, 1), (1, 0)):
            params = (i, j, SWAP_EXCHANGE, POOL_LLAMMA, 2)
            hops.append(Hop(pool, pool.coins[i], pool.coins[j], params))
    return hops


def parse_lending_vaults(
    payload: dict[str, Any],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, float]]]:
    """(ERC4626 vaults, LLAMMAs) of an ``api.curve.finance/v1/getLendingVaults``
    answer: every market is a vault over the borrowed token and a LLAMMA
    between the borrowed token and the collateral."""
    vaults: list[tuple[str, str]] = []
    amms: list[tuple[str, str, str, float]] = []
    for entry in ((payload or {}).get("data") or {}).get("lendingVaultData") or []:
        assets = entry.get("assets") or {}
        borrowed = str((assets.get("borrowed") or {}).get("address") or "").lower()
        collateral = str((assets.get("collateral") or {}).get("address") or "").lower()
        if not borrowed:
            continue
        if entry.get("address"):
            vaults.append((str(entry["address"]).lower(), borrowed))
        if entry.get("ammAddress") and collateral:
            usd = float(entry.get("usdTotal") or 0.0)
            amms.append((str(entry["ammAddress"]).lower(), borrowed, collateral, usd))
    return vaults, amms


def parse_crvusd_markets(payload: dict[str, Any]) -> list[tuple[str, str, str, float]]:
    """LLAMMAs of a ``prices.curve.finance/v1/crvusd/markets/{chain}`` answer."""
    amms: list[tuple[str, str, str, float]] = []
    for entry in (payload or {}).get("data") or []:
        amm = entry.get("llamma")
        collateral = str((entry.get("collateral_token") or {}).get("address") or "").lower()
        stablecoin = str((entry.get("stablecoin_token") or {}).get("address") or "").lower()
        if amm and collateral and stablecoin:
            usd = float(entry.get("collateral_amount_usd") or 0.0) or float("inf")
            amms.append((str(amm).lower(), stablecoin, collateral, usd))
    return amms


class PoolGraph:
    """Pools indexed by coin, with route search up to ``max_hops`` router hops.
    ``extra_hops`` are the hops that are not catalogue pools (wrappers,
    ERC4626 vaults, LLAMMAs)."""

    def __init__(self, pools: Iterable[Pool], extra_hops: Iterable[Hop] = ()):
        self.pools = list(pools)
        self.by_coin: dict[str, list[Pool]] = defaultdict(list)
        self.hops_from: dict[str, list[Hop]] = defaultdict(list)
        by_address = {pool.address.lower(): pool for pool in self.pools}
        for pool in self.pools:
            for coin in pool.coins:
                self.by_coin[coin].append(pool)
            for hop in (*pool_hops(pool), *zap_hops(pool, by_address)):
                if hop.token_in != hop.token_out:
                    self.hops_from[hop.token_in].append(hop)
        for hop in extra_hops:
            self.hops_from[hop.token_in].append(hop)
        for hops in self.hops_from.values():
            hops.sort(key=lambda hop: -hop.pool.usd_total)

    def routes(self, token: str, want: str, *, max_hops: int = 2) -> list[Route]:
        """Candidate routes, deepest pools first (by the shallowest pool on the
        route), fewer hops first among equals, then by the next shallowest
        pools. A route visits a token or a pool once."""
        token, want = token.lower(), want.lower()
        if token == want:
            return []
        max_hops = min(max_hops, MAX_ROUTER_HOPS)
        found: dict[tuple[Any, ...], tuple[Route, tuple[float, ...]]] = {}
        explored = 0

        def walk(at: str, trail: tuple[Hop, ...], tokens: frozenset[str]) -> None:
            nonlocal explored
            for hop in self.hops_from.get(at, []):
                if explored >= MAX_PATHS:
                    return
                if hop.token_out in tokens or any(h.pool is hop.pool for h in trail):
                    continue
                explored += 1
                path = (*trail, hop)
                if hop.token_out == want:
                    flat: list[str] = [token]
                    for step in path:
                        flat += [step.swap or step.pool.address, step.token_out]
                    zaps = tuple(step.zap_pool for step in path)
                    route = Route(
                        tuple(flat),
                        tuple(step.params for step in path),
                        zaps if any(zap != ZERO for zap in zaps) else (),
                    )
                    key = (route.path, route.swap_params, route.zap_pools)
                    depths = tuple(sorted(step.pool.usd_total for step in path))
                    found.setdefault(key, (route, depths))
                elif len(path) < max_hops:
                    walk(hop.token_out, path, tokens | {hop.token_out})

        walk(token, (), frozenset({token}))
        ranked = sorted(
            found.values(),
            key=lambda item: (-item[1][0], item[0].hops, [-depth for depth in item[1]]),
        )
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
        enrich: Callable[[list[Pool]], list[Pool]] | None = None,
        extras: Callable[[list[Pool]], list[Hop]] | None = None,
    ):
        # ``extras`` adds the hops that are not catalogue pools.
        self.extras = extras
        # ``enrich`` completes the parsed pools with what the API does not
        # say (the lending pools' underlying coins, read from the chain).
        self.enrich = enrich
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
            pools = parse_pools(payload, min_usd=self.min_usd)
            if self.enrich is not None:
                pools = self.enrich(pools)
            extra_hops: list[Hop] = []
            if self.extras is not None:
                try:
                    extra_hops = self.extras(pools)
                except Exception as exc:  # noqa: BLE001 - the pool routes still serve
                    log.warning("curve extra hops (wrappers, vaults, LLAMMAs) failed: %s", exc)
            self._graph = PoolGraph(pools, extra_hops)
            self._loaded_at = now
            log.debug("curve catalogue %s: %s routable pools", self.chain, len(self._graph.pools))
        return self._graph


def lending_enricher(client: ChainClient) -> Callable[[list[Pool]], list[Pool]]:
    """Fills ``Pool.underlying`` for lending pools: the legacy registry pools
    whose ``underlying_coins(i)`` answer with coins other than their own."""
    known: dict[str, tuple[str, ...]] = {}

    def underlying(pool: Pool) -> tuple[str, ...]:
        coins: list[str] = []
        for index in range(len(pool.coins)):
            try:
                raw = client.call(pool.address, UNDERLYING_COINS + encode(["uint256"], [index]))
                coins.append(str(decode(["address"], raw)[0]).lower())
            except Exception:  # noqa: BLE001 - not a lending pool (or an older ABI): no hops
                return ()
        return tuple(coins) if tuple(coins) != pool.coins and ZERO not in coins else ()

    def enrich(pools: list[Pool]) -> list[Pool]:
        result = []
        for pool in pools:
            if pool.registry in LENDING_REGISTRIES:
                if pool.address not in known:
                    known[pool.address] = underlying(pool)
                if known[pool.address]:
                    pool = replace(pool, underlying=known[pool.address])
            result.append(pool)
        return result

    return enrich


def extra_hops_provider(
    client: ChainClient,
    router: str,
    chain: str,
    *,
    http: Any | None = None,
    lending_url: str = LENDING_API_URL,
    crvusd_url: str = CRVUSD_API_URL,
) -> Callable[[list[Pool]], list[Hop]]:
    """The hops outside the pool catalogue, each kind kept only where the
    deployed router answers for it: wrappers (swap type 8), ERC4626 vaults
    (9: lending vaults and the pool coins that are vaults) and LLAMMAs."""
    http = http or JsonClient(timeout=60.0)
    router = to_checksum_address(router)
    assets: dict[str, str | None] = {}  # token -> ERC4626 asset, probed once
    confirmed: dict[tuple[str, ...], bool] = {}

    def quotes(path: Sequence[str], params: tuple[int, ...], amount: int) -> bool:
        key = (*path, *map(str, params))
        if key not in confirmed:
            padded = [to_checksum_address(a) for a in path] + [ZERO] * (11 - len(path))
            rows = [list(params)] + [[0] * 5] * 4
            data = GET_DY + encode(
                ["address[11]", "uint256[5][5]", "uint256", "address[5]"],
                [padded, rows, amount, [ZERO] * 5],
            )
            try:
                confirmed[key] = int(decode(["uint256"], client.call(router, data))[0]) > 0
            except Exception:  # noqa: BLE001 - the router does not take this hop
                confirmed[key] = False
        return confirmed[key]

    def fetch(url: str) -> dict[str, Any]:
        try:
            payload = http.request("GET", url)
        except Exception as exc:  # noqa: BLE001 - a source that is down adds no hops
            log.debug("curve catalogue source %s failed: %s", url, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def asset_of(token: str) -> str | None:
        if token not in assets:
            try:
                raw = client.call(to_checksum_address(token), ERC4626_ASSET)
                asset = str(decode(["address"], raw)[0]).lower() if len(raw) == 32 else None
            except Exception:  # noqa: BLE001 - not a vault
                asset = None
            assets[token] = asset if asset and asset != ZERO and asset != token else None
        return assets[token]

    def provide(pools: list[Pool]) -> list[Hop]:
        pairs: list[tuple[str, str, str]] = []
        for wrapped in WRAPPED_NATIVE.get(client.chain_id, ()):
            if quotes((ETH, wrapped, wrapped), (0, 0, SWAP_WRAPPER, 0, 0), 10**15):
                pairs += [(ETH, wrapped, wrapped), (wrapped, ETH, wrapped)]
        if client.chain_id == 1:
            pairs += ETHEREUM_WRAPPERS
        hops = wrapper_hops(pairs)

        vaults, amms = parse_lending_vaults(fetch(f"{lending_url.rstrip('/')}/{chain}"))
        amms += parse_crvusd_markets(fetch(f"{crvusd_url.rstrip('/')}/{chain}"))
        hops += llamma_hops(amms)

        # ERC4626: the router either has swap type 9 or reverts on it. One
        # known vault settles it; without one, only Ethereum's router is
        # assumed to (its source has it, the sidechain one does not).
        if vaults:
            vault, asset = vaults[0]
            has_erc4626 = quotes((asset, vault, vault), (0, 0, SWAP_ERC4626, 0, 0), 10**6)
        else:
            has_erc4626 = client.chain_id == 1
        if has_erc4626:
            known = dict(vaults)
            tokens = {coin for pool in pools for coin in pool.coins} - {ETH}
            for token in sorted(tokens - set(known)):
                asset = asset_of(token)
                if asset is not None:
                    known[token] = asset
            hops += erc4626_hops(known.items())
        return hops

    return provide


class CurveRouter:
    """Quotes and swap calls through the Curve router NG for the best route."""

    def __init__(
        self,
        client: ChainClient,
        address: str,
        graph: Callable[[], PoolGraph],
        *,
        max_hops: int = MAX_ROUTER_HOPS,
        max_candidates: int = 16,
        routes: dict[str, Route] | None = None,
        route_ttl: float = 120.0,
    ):
        # The chosen route per (token, want) is kept for ``route_ttl`` seconds:
        # a lot polled at its trigger costs one quote, not a route search.
        self.route_ttl = route_ttl
        self._best: dict[tuple[str, str], tuple[Route, bool, float]] = {}
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
            [path, params, amount, self._zap_pools(route)],
        )
        try:
            return int(decode(["uint256"], self.client.call(self.address, data))[0])
        except CallReverted as exc:
            log.debug("router get_dy reverted for %s: %s", route, exc)
            return 0

    @staticmethod
    def _zap_pools(route: Route) -> list[str]:
        pools = [to_checksum_address(pool) for pool in route.zap_pools]
        return pools + [ZERO] * (5 - len(pools))

    def candidates(self, token: str, want: str) -> list[Route]:
        explicit = self.routes.get(token.lower())
        if explicit is not None:
            return [explicit]
        return self.graph().routes(token, want, max_hops=self.max_hops)

    def best_route(self, token: str, want: str, amount: int) -> tuple[Route, int] | None:
        """The candidate paying the most want for ``amount``; None when nothing
        routes. Candidates are tried in rank order until ``max_candidates`` of
        them pay (hops are generated generously, so some revert), looking at
        no more than four times as many. When none takes the whole amount
        (it drains a pool on the way) the choice is made at the chunk
        search's probe size instead and the quote is that price for the
        whole amount: what the lot is worth taken in chunks."""
        key = (token.lower(), want.lower())
        probe = max(amount // PROBE_FRACTION, 1)
        cached = self._best.get(key)
        if cached is not None and time.monotonic() - cached[2] < self.route_ttl:
            route, whole = cached[0], cached[1]
            dy = self.get_dy(route, amount if whole else probe)
            if dy > 0:
                return (route, dy) if whole else (route, dy * amount // probe)
        self._best.pop(key, None)
        candidates = self.candidates(token, want)[: self.max_candidates * 4]
        for size in (amount, probe):
            best: tuple[Route, int] | None = None
            paying = 0
            for route in candidates:
                dy = self.get_dy(route, size)
                if dy <= 0:
                    continue
                paying += 1
                if best is None or dy > best[1]:
                    best = (route, dy)
                if paying >= self.max_candidates:
                    break
            if best is not None:
                self._best[key] = (best[0], size == amount, time.monotonic())
                return best if size == amount else (best[0], best[1] * amount // size)
        return None

    def exchange_call(self, route: Route, amount: int, min_dy: int, receiver: str) -> Call:
        return curve_router_exchange(
            self.address,
            list(route.path),
            [list(p) for p in route.swap_params],
            amount,
            min_dy,
            receiver,
            pools=self._zap_pools(route),
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
    catalog = PoolCatalog(
        chain,
        http=http,
        api_url=settings.api_url,
        min_usd=settings.min_pool_usd,
        enrich=lending_enricher(client),
        extras=extra_hops_provider(client, address, chain, http=http),
    )
    return CurveRouter(
        client, address, catalog.graph, max_hops=settings.max_hops, routes=settings.routes
    )


def route_from_config(raw: Sequence[Sequence[Any]] | dict[str, Any]) -> Route:
    """``{path: [token, pool, token, ...], params: [[i, j, swap, pool_type, n], ...]}``,
    with ``pools: [pool or zero per hop]`` for hops through a zap (swap type 3)."""
    path = tuple(to_checksum_address(a) for a in raw["path"])
    params = tuple(tuple(int(x) for x in row) for row in raw["params"])
    if len(path) < 3 or len(path) % 2 == 0 or len(params) != len(path) // 2:
        raise ValueError("route path must be token, pool, token, ... with one params row per hop")
    if any(len(row) != 5 for row in params):
        raise ValueError("each swap params row has five entries")
    zaps = tuple(to_checksum_address(a) for a in (raw.get("pools") or ()))
    if zaps and len(zaps) != len(params):
        raise ValueError("route pools must have one entry per hop")
    return Route(path, params, zaps)  # type: ignore[arg-type]


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
