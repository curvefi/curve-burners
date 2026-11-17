"""The native rail against the real AuctionTaker and a Curve router stand-in."""

from types import SimpleNamespace

import boa
import pytest

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import Auction
from keepers.auction_keeper.config import NativeSettings, PricingSettings, TokenFilter
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.core.schedule import band_entry
from keepers.auction_keeper.pricing import CurveRouterSource, build_sources
from keepers.auction_keeper.rails.native import BestRouter, NativeTaker, Routed
from keepers.auction_keeper.routing.curve_router import CurveRouter, PoolCatalog
from tests.keeper.conftest import LOT_AMOUNT

WAD = 10**18
RATE = WAD // 10  # 0.1 want per sell token


@pytest.fixture
def client(keeper_account):
    return BoaClient(sender=keeper_account)


@pytest.fixture
def taker():
    return boa.load("contracts/burners/auction/periphery/AuctionTaker.vy")


@pytest.fixture
def router_mock(want):
    router = boa.load("contracts/testing/dutch_auction/RouterMock.vy")
    want._mint_for_testing(router.address, 10**6 * WAD)
    return router


@pytest.fixture
def catalog(http, sell_token, want):
    pool = "0x" + "01" * 20
    http.on(
        "GET",
        "/getPools/all/boa",
        {
            "data": {
                "poolData": [
                    {
                        "address": pool,
                        "name": "CRV/crvUSD",
                        "coins": [{"address": sell_token.address}, {"address": want.address}],
                        "registryId": "factory-twocrypto",
                        "usdTotal": 100_000,
                    }
                ]
            }
        },
    )
    return PoolCatalog("boa", http=http, min_usd=1000)


@pytest.fixture
def router(client, router_mock, catalog):
    return CurveRouter(client, router_mock.address, catalog.graph)


@pytest.fixture
def native_config(network_config, taker, sell_token):
    def _config(**native):
        return network_config(
            tokens=TokenFilter(whitelist=frozenset([sell_token.address])),
            mode="keeper",
            auction_taker=taker.address,
            native=NativeSettings(enabled=True, chain="boa", **native),
        )

    return _config


def keeper_with(config, client, state, router, taker, **kwargs):
    """A keeper with the native rail only (no publisher classes: the harness's
    CoW adapter is reported as unknown and skipped)."""
    native = NativeTaker(config, client, router, taker=taker.address)
    return NetworkKeeper(config, state, client=client, native=native, publishers={}, **kwargs)


def travel_to(timestamp):
    boa.env.time_travel(seconds=max(0, timestamp - boa.env.evm.patch.timestamp))


def test_take_when_the_curve_crosses_the_route_quote(
    client,
    state,
    native_config,
    router,
    router_mock,
    taker,
    harness,
    sell_token,
    want,
    proceeds_receiver,
    keeper_account,
    lot,
):
    router_mock.set_pair(sell_token.address, want.address, RATE, 0)
    config = native_config()
    keeper = keeper_with(config, client, state, router, taker)
    auction = Auction(client, harness.address)

    (outcome,) = keeper.cycle()
    assert outcome.action == "waiting" and outcome.adapter == "native"
    assert "route_dy=" in outcome.detail
    assert keeper.wait_seconds(0) == config.poll_interval  # far from the route: slow poll

    # Just above the band: the loop tightens its poll.
    dy = LOT_AMOUNT * RATE // WAD
    view = auction.lot_view(sell_token.address)
    travel_to(band_entry(auction, view, int(dy * 1.15), view.timestamp, view.end))
    (outcome,) = keeper.cycle()
    assert outcome.action == "waiting" and keeper.wait_seconds(0) == config.native.poll

    # Under the route quote by the margin: taken, the auction paid from the
    # swap, the profit to the keeper, the lot gone.
    view = auction.lot_view(sell_token.address)
    travel_to(
        band_entry(auction, view, int(dy * (1 - config.native.margin)), view.timestamp, view.end)
    )
    (outcome,) = keeper.cycle()
    assert outcome.action == "taken", outcome.detail
    payment = int(outcome.detail.split("payment=")[1].split()[0])
    assert want.balanceOf(proceeds_receiver) == payment
    assert want.balanceOf(keeper_account) == dy - payment > 0
    assert sell_token.balanceOf(router_mock.address) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0
    (outcome,) = keeper.cycle()
    assert outcome.action == "skipped" and outcome.detail == "inactive lot"


def test_thin_pool_is_taken_in_chunks(
    client, state, native_config, router, router_mock, taker, harness, sell_token, want, lot
):
    # Depth equal to the lot: taking it all would cost 50%; 1% impact allows ~1%.
    router_mock.set_pair(sell_token.address, want.address, RATE, LOT_AMOUNT)
    config = native_config(max_impact=0.01)
    keeper = keeper_with(config, client, state, router, taker)
    auction = Auction(client, harness.address)

    quote = keeper.native.quote(auction, auction.lot_view(sell_token.address))
    assert quote is not None
    assert quote.amount < LOT_AMOUNT and quote.impact <= 0.01
    assert LOT_AMOUNT * 0.005 < quote.amount < LOT_AMOUNT * 0.02

    view = auction.lot_view(sell_token.address)
    travel_to(band_entry(auction, view, 1, view.timestamp, view.end) - 7200)
    (outcome,) = keeper.cycle()
    assert outcome.action == "taken", outcome.detail
    assert 0 < harness.available(sell_token.address) <= LOT_AMOUNT - quote.amount
    assert sell_token.balanceOf(router_mock.address) == LOT_AMOUNT - harness.available(
        sell_token.address
    )

    # The next chunk is quoted on what is left.
    again = keeper.native.quote(auction, auction.lot_view(sell_token.address))
    assert again is not None and again.amount <= harness.available(sell_token.address)


def test_offline_and_dry_run_only_simulate(
    client, state, native_config, router, router_mock, taker, harness, sell_token, want, lot
):
    router_mock.set_pair(sell_token.address, want.address, RATE, 0)
    auction = Auction(client, harness.address)
    view = auction.lot_view(sell_token.address)
    travel_to(band_entry(auction, view, 1, view.timestamp, view.end) - 7200)

    offline = keeper_with(
        native_config().__class__(**{**native_config().__dict__, "mode": "offline"}),
        client,
        state,
        router,
        taker,
    )
    (outcome,) = offline.cycle()
    assert outcome.action == "simulated" and client.sent == []

    dry = keeper_with(native_config(), client, state, router, taker, dry_run=True)
    (outcome,) = dry.cycle()
    assert outcome.action == "simulated" and client.sent == []
    assert harness.available(sell_token.address) == LOT_AMOUNT


def test_unroutable_lot_is_reported(
    client, state, native_config, router, taker, harness, erc20_deployer, stage
):
    other = erc20_deployer.deploy("Orphan", "ORPH", 18)
    stage(other)
    config = native_config()
    config = config.__class__(
        **{**config.__dict__, "tokens": TokenFilter(whitelist=frozenset([other.address]))}
    )
    keeper = keeper_with(config, client, state, router, taker)
    (outcome,) = keeper.cycle()
    assert outcome.action == "unroutable"


def test_router_price_source_quotes_the_best_route(
    client, router, router_mock, sell_token, want, harness, lot, network_config
):
    router_mock.set_pair(sell_token.address, want.address, RATE, 0)
    source = CurveRouterSource(router)
    view = Auction(client, harness.address).lot_view(sell_token.address)
    assert source.quote(view) == LOT_AMOUNT * RATE // WAD
    built = build_sources(
        network_config(
            pricing=PricingSettings(sources=("router",)),
            native=NativeSettings(router=router_mock.address, chain="boa"),
        ),
        client,
    )
    assert [type(s) for s in built] == [CurveRouterSource]
    assert built[0].router.address == router_mock.address


class StubRouter:
    """A router answering a fixed want; ``fails`` names the calls that raise."""

    def __init__(self, dy, *, fails=(), chunks=True):
        self.dy = dy
        self.fails = fails
        self.chunks = chunks

    def best_route(self, token, want, amount):
        if "best_route" in self.fails:
            raise RuntimeError("down")
        return ("route", self.dy) if self.dy else None

    def chunk(self, route, available, max_impact):
        if "chunk" in self.fails:
            raise RuntimeError("down")
        return (route, available // 2, self.dy // 2, 0.0) if self.chunks else None

    def swap_calls(self, route, amount, min_dy, receiver):
        return [(self.dy, route, amount, min_dy, receiver)]


@pytest.mark.parametrize("parallel", [True, False])
def test_best_router_swaps_through_the_router_paying_the_most(parallel):
    low, high, none = StubRouter(90), StubRouter(100), StubRouter(0)
    down = StubRouter(500, fails=("best_route",))
    best = BestRouter([low, high, none, down], parallel=parallel)
    route, dy = best.best_route("0xT", "0xW", 10)
    assert dy == 100 and route.router is high and str(route) == "route"
    assert route.others == ((low, "route"),)
    # The chunk and the calls are the winner's own.
    chunked, amount, chunk_dy, impact = best.chunk(route, 10, 0.01)
    assert (chunked, amount, chunk_dy, impact) == (Routed(high, "route"), 5, 50, 0.0)
    assert best.swap_calls(chunked, 5, 49, "0xR") == [(100, "route", 5, 49, "0xR")]
    # One router left standing is enough; none is no route.
    assert BestRouter([down, low], parallel=parallel).best_route("0xT", "0xW", 10)[1] == 90
    assert BestRouter([down, none], parallel=parallel).best_route("0xT", "0xW", 10) is None


def test_best_router_falls_back_when_the_winner_cannot_chunk():
    low = StubRouter(90)
    for broken in (StubRouter(100, fails=("chunk",)), StubRouter(100, chunks=False)):
        best = BestRouter([low, broken])
        route, dy = best.best_route("0xT", "0xW", 10)
        assert route.router is broken and dy == 100
        chunked, amount, chunk_dy, _ = best.chunk(route, 10, 0.01)
        assert chunked.router is low and (amount, chunk_dy) == (5, 45)
    alone = BestRouter([StubRouter(100, chunks=False), StubRouter(0)])
    assert alone.chunk(alone.best_route("0xT", "0xW", 10)[0], 10, 0.01) is None


def test_backends_are_built_from_the_config_list(client, network_config, taker, monkeypatch):
    from keepers.auction_keeper.rails import native as rail

    curve, electric = StubRouter(1), StubRouter(2)
    monkeypatch.setitem(rail.BACKENDS, "curve", lambda config, client: curve)
    monkeypatch.setitem(rail.BACKENDS, "electric", lambda config, client: electric)

    def config(*backends):
        return network_config(
            auction_taker=taker.address, native=NativeSettings(enabled=True, backends=backends)
        )

    assert rail.router_for(config("electric"), client) is electric
    both = rail.router_for(config("electric", "curve"), client)
    assert isinstance(both, BestRouter) and both.routers == [electric, curve]


def test_lots_are_quoted_in_parallel_and_one_failure_spares_the_rest(
    client, state, native_config, router, taker
):
    import threading

    keeper = keeper_with(native_config(), client, state, router, taker)
    barrier = threading.Barrier(3, timeout=10)
    lot = SimpleNamespace(token="0xA", active=True)
    gone = SimpleNamespace(token="0xD", active=False)

    class Reader:
        def lot_view(self, token):
            if token == "0xbad":
                raise RuntimeError("rpc down")
            return lot

    def try_quote(auction, lot):
        barrier.wait()  # passes only when three quotes run at once
        return ("quote", threading.current_thread().name)

    keeper.native.try_quote = try_quote
    keeper.client.concurrent = True
    reader = Reader()
    answers = keeper._quote_all(
        [(reader, lot), (reader, "0xread"), (reader, "0xbad"), (reader, gone), (reader, lot)]
    )
    assert [a[0] for a in answers] == [lot, lot, None, gone, lot]
    assert answers[2] == (None, None) and answers[3] == (gone, None)
    threads = {a[1][1] for a in answers if a[1]}
    assert len(threads) == 3 and all(name.startswith("quote") for name in threads)
