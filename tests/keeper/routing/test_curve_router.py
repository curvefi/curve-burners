"""Route search over the Curve pool catalogue (pure Python)."""

import pytest

from keepers.auction_keeper.routing.curve_router import (
    PoolGraph,
    Route,
    parse_pools,
    pool_type,
    route_from_config,
)

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20
C = "0x" + "cc" * 20
D = "0x" + "dd" * 20
P_AB = "0x" + "01" * 20
P_BC = "0x" + "02" * 20
P_AC = "0x" + "03" * 20
P_ABC = "0x" + "04" * 20
P_CD = "0x" + "05" * 20


def entry(address, coins, registry, usd, **extra):
    return {
        "address": address,
        "name": address[:6],
        "coins": [{"address": c} for c in coins],
        "registryId": registry,
        "usdTotal": usd,
        **extra,
    }


PAYLOAD = {
    "data": {
        "poolData": [
            entry(P_AB, [A, B], "factory-stable-ng", 500_000),
            entry(P_BC, [B, C], "factory-twocrypto", 50_000),
            entry(P_AC, [A, C], "factory-crypto", 800),  # below the TVL floor
            entry(P_ABC, [A, B, C], "factory-tricrypto", 20_000),
            entry(P_CD, [C, D], "factory-stable-ng", 9_000, isBroken=True),
            entry("0x" + "06" * 20, [A, D], "lending", 1_000_000),  # unknown kind
        ]
    }
}


def test_pool_types_follow_the_registry():
    assert pool_type({"registryId": "factory-stable-ng", "coins": [1, 2]}) == 1
    assert pool_type({"registryId": "main", "coins": [1, 2, 3]}) == 1
    assert pool_type({"registryId": "factory-twocrypto", "coins": [1, 2]}) == 2
    assert pool_type({"registryId": "factory-crypto", "coins": [1, 2]}) == 2
    assert pool_type({"registryId": "factory-tricrypto", "coins": [1, 2, 3]}) == 3
    assert pool_type({"registryId": "crypto", "coins": [1, 2, 3]}) == 3
    assert pool_type({"registryId": "crypto", "coins": [1, 2]}) == 2
    assert pool_type({"registryId": "lending", "coins": [1, 2]}) is None


def test_parse_pools_filters_kind_tvl_and_broken():
    pools = parse_pools(PAYLOAD, min_usd=1000)
    assert [p.address.lower() for p in pools] == [P_AB, P_BC, P_ABC]
    assert pools[2].pool_type == 3 and pools[2].coins == (A, B, C)
    assert len(parse_pools(PAYLOAD)) == 4  # the thin pool is back without a floor


def test_routes_direct_and_two_hop_ranked_by_depth():
    graph = PoolGraph(parse_pools(PAYLOAD, min_usd=1000))
    routes = graph.routes(A, C)
    pools = [tuple(p.lower() for p in r.pools) for r in routes]
    # Deepest first (the shallowest pool on the route): A -P_AB-> B -P_BC-> C
    # (50k) beats the three 20k routes, among which the direct hop comes first.
    assert pools == [(P_AB, P_BC), (P_ABC,), (P_AB, P_ABC), (P_ABC, P_BC)]
    assert routes[0].path == (A, routes[0].pools[0], B, routes[0].pools[1], C)
    assert routes[0].swap_params == ((0, 1, 1, 1, 2), (0, 1, 1, 2, 2))
    assert routes[1].swap_params == ((0, 2, 1, 3, 3),)
    assert routes[2].swap_params == ((0, 1, 1, 1, 2), (1, 2, 1, 3, 3))
    assert graph.routes(A, C, max_hops=1) == [routes[1]]
    assert graph.routes(C, A, max_hops=1)[0].swap_params == ((2, 0, 1, 3, 3),)  # reversed
    assert graph.routes(A, D) == [] and graph.routes(A, A) == []


def test_route_from_config_validates_shape():
    route = route_from_config({"path": [A, P_AB, B], "params": [[0, 1, 1, 1, 2]]})
    assert isinstance(route, Route) and route.hops == 1 and route.pools == (route.path[1],)
    with pytest.raises(ValueError):
        route_from_config({"path": [A, P_AB], "params": [[0, 1, 1, 1, 2]]})
    with pytest.raises(ValueError):
        route_from_config({"path": [A, P_AB, B], "params": [[0, 1, 1]]})
