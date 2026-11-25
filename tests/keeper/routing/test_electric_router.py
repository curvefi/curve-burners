"""The electric router backend: the ``erouter`` report, chunking, and a take
through the real AuctionTaker with the tool replaced by a stand-in."""

import json
import subprocess
from types import SimpleNamespace

import boa
import pytest

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import Auction
from keepers.auction_keeper.config import (
    ElectricSettings,
    NativeSettings,
    PricingSettings,
    TokenFilter,
)
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.execute import curve_router_exchange
from keepers.auction_keeper.pricing import ElectricRouterSource, build_sources
from keepers.auction_keeper.rails.native import NativeTaker, router_for
from keepers.auction_keeper.routing.electric_router import (
    TAKER_MAX_CALL_DATA,
    ElectricRouter,
    ElectricRouterError,
    electric_router_for,
)
from tests.keeper.conftest import LOT_AMOUNT, band_entry

WAD = 10**18
RATE = WAD // 10  # 0.1 want per sell token
TOKEN = "0x" + "aa" * 20
WANT = "0x" + "bb" * 20
TAKER = "0x" + "ee" * 20
ROUTER = "0xf5438dafc165b466f4a61ce57bd3aa59bcd5979e"
POOL = "0x" + "01" * 20


class FakeErouter:
    """Stands in for ``subprocess.run``: writes the report ``erouter route
    --json`` would. ``answer(token, want, amount, receiver)`` returns the
    report, None for a clean exit without one, or raises."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["timeout"] > 0 and kwargs["capture_output"]

        def flag(name):
            return argv[argv.index(name) + 1] if name in argv else None

        payload = self.answer(
            flag("--from"), flag("--to"), int(flag("--amount-wei")), flag("--receiver")
        )
        if payload is None:
            return SimpleNamespace(returncode=0, stdout="no route between the tokens\n", stderr="")
        with open(flag("--json"), "w") as handle:
            json.dump(payload, handle)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def report(token, want, amount, receiver, *, dy, impact_bp=1.0, data="0x" + "00" * 260, **call):
    payload = {
        "chain": {"name": "boa", "chain_id": 1, "block": 123},
        "result": {
            "verified": True,
            "verified_out": str(dy),
            "price_impact": None if impact_bp is None else {"bp": impact_bp},
        },
    }
    if receiver is not None:
        payload["call"] = {
            "to": ROUTER,
            "calldata": data,
            "amount_in": str(amount),
            "token_in": token,
            "token_out": want,
            "pools": [POOL],
            "receiver": receiver,
            "guaranteed_out": str(dy * 9995 // 10000),
            "unbounded_legs": [],
            **call,
        }
    return payload


def flat(token, want, amount, receiver):
    return report(token, want, amount, receiver, dy=amount // 10)


def test_quote_runs_the_tool_and_reads_the_report():
    tool = FakeErouter(flat)
    router = ElectricRouter(
        "fraxtal",
        taker=TAKER,
        command=("/opt/er/bin/erouter",),
        rpc_url="https://rpc",
        slippage_bp=30,
        min_tvl=5000,
        run=tool,
    )
    route, dy = router.best_route(TOKEN.upper().replace("0X", "0x"), WANT, 1000)
    assert dy == 100 and route.amount == 1000 and route.impact == 1e-4
    assert route.router.lower() == ROUTER and len(route.data) == 260 and route.block == 123
    (argv,) = tool.calls
    assert argv[:2] == ["/opt/er/bin/erouter", "route"]
    for name, value in {
        "--chain": "fraxtal",
        "--from": TOKEN,
        "--to": WANT,
        "--amount-wei": "1000",
        "--max-legs": "16",
        "--calldata": "needed",
        "--receiver": router.taker,
        "--slippage-bp": "30",
        "--rpc-url": "https://rpc",
        "--min-tvl": "5000",
    }.items():
        assert argv[argv.index(name) + 1] == value

    approve, swap = router.swap_calls(route, 1000, 90, TAKER)
    assert approve.target.lower() == TOKEN and swap.target.lower() == ROUTER
    assert swap.data == route.data


def test_quotes_are_cached_per_amount():
    now = [0.0]
    tool = FakeErouter(flat)
    router = ElectricRouter("fraxtal", taker=TAKER, cache=30, run=tool, clock=lambda: now[0])
    assert router.quote(TOKEN, WANT, 1000) is router.quote(TOKEN, WANT, 1000)
    router.quote(TOKEN, WANT, 2000)
    assert len(tool.calls) == 2
    now[0] = 31.0
    router.quote(TOKEN, WANT, 1000)
    assert len(tool.calls) == 3


def test_quote_only_without_a_taker():
    tool = FakeErouter(flat)
    router = ElectricRouter("fraxtal", run=tool)
    route, dy = router.best_route(TOKEN, WANT, 1000)
    assert dy == 100 and route.router is None
    assert "--calldata" not in tool.calls[0] and "--receiver" not in tool.calls[0]
    with pytest.raises(ElectricRouterError, match="quote-only"):
        router.swap_calls(route, 1000, 0, TAKER)


def test_nothing_routes():
    router = ElectricRouter("fraxtal", taker=TAKER, run=FakeErouter(lambda *a: None))
    assert router.best_route(TOKEN, WANT, 1000) is None

    # The tool's own "no route" exit: code 2 with the reason on stdout.
    def unroutable(argv, **kwargs):
        said = "\x1b[31m✘\x1b[0m token not routable in this universe\n"
        return SimpleNamespace(returncode=2, stdout=said, stderr="")

    router = ElectricRouter("fraxtal", taker=TAKER, run=unroutable)
    assert router.best_route(TOKEN, WANT, 1000) is None
    assert router.best_route(TOKEN, TOKEN, 1000) is None


@pytest.mark.parametrize(
    "change",
    [
        {"amount_in": "999"},
        {"token_in": WANT},
        {"token_out": TOKEN},
        {"receiver": "0x" + "dd" * 20},
        {"to": "nope"},
        {"to": "0x" + "cc" * 20},  # not the pinned router
    ],
)
def test_a_report_for_another_trade_is_refused(change):
    def answer(token, want, amount, receiver):
        payload = report(token, want, amount, receiver, dy=100)
        payload["call"].update(change)
        return payload

    router = ElectricRouter("fraxtal", taker=TAKER, router=ROUTER, run=FakeErouter(answer))
    with pytest.raises(ElectricRouterError):
        router.quote(TOKEN, WANT, 1000)


def test_unusable_reports_are_dropped():
    def too_long(token, want, amount, receiver):
        data = "0x" + "00" * (TAKER_MAX_CALL_DATA + 1)
        return report(token, want, amount, receiver, dy=100, data=data)

    def unverified(token, want, amount, receiver):
        payload = report(token, want, amount, receiver, dy=100)
        payload["result"]["verified"] = False
        return payload

    def no_call(token, want, amount, receiver):
        payload = report(token, want, amount, receiver, dy=100)
        del payload["call"]
        return payload

    for answer in (too_long, unverified, no_call):
        router = ElectricRouter("fraxtal", taker=TAKER, run=FakeErouter(answer))
        assert router.quote(TOKEN, WANT, 1000) is None


def test_a_broken_tool_is_an_error():
    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def crashed(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nKeyError: 'FRAXTAL'\n")

    def bad_flag(argv, **kwargs):  # argparse also exits 2, but says so on stderr
        return SimpleNamespace(returncode=2, stdout="", stderr="erouter: error: bad --chain\n")

    def no_node(argv, **kwargs):
        return SimpleNamespace(returncode=4, stdout="✘ warm failed: rpc unreachable\n", stderr="")

    for run, match in (
        (missing, "not found"),
        (slow, "timed out"),
        (crashed, "KeyError"),
        (bad_flag, "bad --chain"),
        (no_node, "exited 4: ✘ warm failed"),
    ):
        router = ElectricRouter("fraxtal", taker=TAKER, run=run)
        with pytest.raises(ElectricRouterError, match=match):
            router.quote(TOKEN, WANT, 1000)


def test_chunk_shrinks_by_the_reported_impact():
    # Impact linear in size: 400 bp for the whole lot of 1000.
    def answer(token, want, amount, receiver):
        return report(token, want, amount, receiver, dy=amount // 10, impact_bp=amount * 0.4)

    tool = FakeErouter(answer)
    router = ElectricRouter("fraxtal", taker=TAKER, run=tool)
    whole, _ = router.best_route(TOKEN, WANT, 1000)
    route, amount, dy, impact = router.chunk(whole, 1000, 0.01)
    assert amount == 225 and route.amount == 225 and dy == 22  # 1000 * (100 / 400) * 0.9
    assert impact <= 0.01 and len(tool.calls) == 2
    # Under the limit, or impact not measured: the whole amount, no further run.
    assert router.chunk(whole, 1000, 0.05)[1] == 1000
    assert len(tool.calls) == 2

    unmeasured = ElectricRouter(
        "fraxtal",
        taker=TAKER,
        run=FakeErouter(lambda *a: report(*a, dy=100, impact_bp=None)),
    )
    whole, _ = unmeasured.best_route(TOKEN, WANT, 1000)
    assert unmeasured.chunk(whole, 1000, 0.01) == (whole, 1000, 100, 0.0)


def test_chunk_gives_up_when_impact_never_fits():
    stubborn = FakeErouter(lambda *a: report(*a, dy=100, impact_bp=500.0))
    router = ElectricRouter("fraxtal", taker=TAKER, run=stubborn)
    whole, _ = router.best_route(TOKEN, WANT, 10**6)
    assert router.chunk(whole, 10**6, 0.01) is None


# Against the real AuctionTaker: the tool's call is opaque to the keeper, so
# the stand-in emits a RouterMock exchange in place of ElectricRouter.execute.


@pytest.fixture
def client(keeper_account):
    return BoaClient(sender=keeper_account)


@pytest.fixture
def taker():
    return boa.load("contracts/burners/auction/periphery/AuctionTaker.vy")


@pytest.fixture
def router_mock(want, sell_token):
    router = boa.load("contracts/testing/dutch_auction/RouterMock.vy")
    want._mint_for_testing(router.address, 10**6 * WAD)
    router.set_pair(sell_token.address, want.address, RATE, 0)
    return router


@pytest.fixture
def erouter(router_mock):
    def answer(token, want, amount, receiver):
        call = curve_router_exchange(
            router_mock.address, [token, POOL, want], [[0, 1, 1, 2, 2]], amount, 0, receiver
        )
        return report(
            token,
            want,
            amount,
            receiver,
            dy=amount * RATE // WAD,
            data="0x" + call.data.hex(),
            to=router_mock.address,
        )

    return FakeErouter(answer)


@pytest.fixture
def native_config(network_config, taker, sell_token):
    return network_config(
        tokens=TokenFilter(whitelist=frozenset([sell_token.address])),
        mode="keeper",
        auction_taker=taker.address,
        native=NativeSettings(enabled=True, backends=("electric",)),
    )


def travel_to(timestamp):
    boa.env.time_travel(seconds=max(0, timestamp - boa.env.evm.patch.timestamp))


def test_take_through_the_electric_router(
    client,
    state,
    native_config,
    erouter,
    router_mock,
    taker,
    harness,
    sell_token,
    want,
    proceeds_receiver,
    keeper_account,
    lot,
):
    router = ElectricRouter("boa", taker=taker.address, run=erouter)
    native = NativeTaker(native_config, client, router, taker=taker.address)
    keeper = NetworkKeeper(native_config, state, client=client, native=native, publishers={})
    auction = Auction(client, harness.address)

    (outcome,) = keeper.cycle()
    assert outcome.action == "waiting" and "electric router, 1 leg(s)" in outcome.detail

    dy = LOT_AMOUNT * RATE // WAD
    view = auction.lot_view(sell_token.address)
    margin = native_config.native.margin
    travel_to(band_entry(auction, view, int(dy * (1 - margin))))
    (outcome,) = keeper.cycle()
    assert outcome.action == "taken", outcome.detail
    payment = int(outcome.detail.split("payment=")[1].split()[0])
    assert want.balanceOf(proceeds_receiver) == payment
    assert want.balanceOf(keeper_account) == dy - payment > 0
    assert sell_token.balanceOf(router_mock.address) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0
    # Nothing rests on the taker, and the router's allowance is spent.
    assert sell_token.balanceOf(taker.address) == want.balanceOf(taker.address) == 0
    assert sell_token.allowance(taker.address, router_mock.address) == 0


def test_a_route_that_underpays_fails_in_simulation(
    client, state, native_config, erouter, router_mock, taker, harness, sell_token, want, lot
):
    # The report promises the full rate; the pool moved and now pays a tenth.
    router = ElectricRouter("boa", taker=taker.address, run=erouter)
    native = NativeTaker(native_config, client, router, taker=taker.address)
    keeper = NetworkKeeper(native_config, state, client=client, native=native, publishers={})
    auction = Auction(client, harness.address)
    dy = LOT_AMOUNT * RATE // WAD
    view = auction.lot_view(sell_token.address)
    travel_to(band_entry(auction, view, int(dy * 0.99)))
    router_mock.set_pair(sell_token.address, want.address, RATE // 10, 0)

    (outcome,) = keeper.cycle()
    assert outcome.action == "failed" and "simulation" in outcome.detail
    assert client.sent == [] and harness.available(sell_token.address) == LOT_AMOUNT


def test_backend_and_price_source_come_from_the_config(client, network_config, taker):
    config = network_config(
        auction_taker=taker.address,
        pricing=PricingSettings(sources=("electric",)),
        native=NativeSettings(
            enabled=True,
            backends=("electric",),
            electric=ElectricSettings(command=("er",), chain="fraxtal", max_legs=8),
        ),
    )
    router = router_for(config, client)
    assert isinstance(router, ElectricRouter)
    assert (router.chain, router.command, router.max_legs) == ("fraxtal", ("er",), 8)
    assert router.taker == taker.address
    # The price source shares the instance, and with it the quote cache.
    (source,) = build_sources(config, client)
    assert isinstance(source, ElectricRouterSource) and source.router is router
    assert electric_router_for(config, client) is router
