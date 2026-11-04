"""The native rail against the real AuctionTaker and a Curve router stand-in."""

import boa
import pytest

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import Auction
from keepers.auction_keeper.config import NativeSettings, PricingSettings, TokenFilter
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.core.schedule import band_entry
from keepers.auction_keeper.pricing import CurveRouterSource, build_sources
from keepers.auction_keeper.rails.native import NativeTaker
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
