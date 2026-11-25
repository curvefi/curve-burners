"""A take pays for its gas, and buys gas for the account that runs short."""

from types import SimpleNamespace

import pytest
from eth_abi import decode, encode

from keepers.auction_keeper.chain.abi import CallReverted
from keepers.auction_keeper.chain.client import GasQuote
from keepers.auction_keeper.config import NativeSettings, NetworkConfig, parse_config
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.core.state import KeeperState
from keepers.auction_keeper.execute import Call
from keepers.auction_keeper.pricing import MarketPricer
from keepers.auction_keeper.rails.native import NATIVE_TOKEN, NativeTaker, TakeQuote
from tests.keeper.pure.conftest import AUCTION, WAD, WANT, FakeChain, token_address

TAKER = token_address(10**6)
KEEPER = token_address(10**6 + 1)
SECOND = token_address(10**6 + 2)
TOKEN = token_address(1)
ROUTER = token_address(10**6 + 9)
GWEI = 10**9
ETH_PRICE = 3000.0  # want (a dollar token) per native token
ROUTE_TYPES = ["address", "address", "uint256", "uint256", "address", "bytes"]


class Chain(FakeChain):
    sender = KEEPER

    def __init__(self, gas=300_000, base_fee=5 * GWEI):
        super().__init__()
        self.quote = GasQuote(gas, base_fee, GWEI // 10)
        self.quoted: list[bytes] = []
        self.sent: list[tuple[bytes, dict]] = []
        self.refill_reverts = False

    def gas_quote(self, to, data, *, value=0, at=None):
        self.quoted.append(bytes(data))
        if self.refill_reverts and len(route_of(data)) > 2:
            raise CallReverted(b"")
        return self.quote

    def send(self, to, data, **kwargs):
        self.sent.append((bytes(data), kwargs))
        return SimpleNamespace(tx_hash="0xabc", ok=True)


def route_of(data):
    """The (target, calldata) steps packed in a take's route."""
    packed, steps = decode(ROUTE_TYPES, bytes(data)[4:])[5], []
    while packed:
        size = int.from_bytes(packed[20:22], "big")
        steps.append(("0x" + packed[:20].hex(), packed[22 : 22 + size]))
        packed = packed[22 + size :]
    return steps


def min_profit_of(data):
    return decode(ROUTE_TYPES, bytes(data)[4:])[3]


class Router:
    """Two steps per swap, as the Curve router's approve + exchange."""

    def __init__(self):
        self.asked = []

    def best_route(self, token, want, amount):
        self.asked.append((token, want, amount))
        return ("route", amount)

    def swap_calls(self, route, amount, min_dy, receiver):
        marker = encode(["uint256", "uint256", "address"], [amount, min_dy, receiver])
        return [Call(ROUTER, b"\\x01"), Call(ROUTER, marker)]


def taker_of(chain, **native):
    cfg = NetworkConfig(
        name="fake",
        rpc="fake://",
        auctions=(AUCTION,),
        auction_taker=TAKER,
        native=NativeSettings(enabled=True, margin=0.0, **native),
    )
    return NativeTaker(cfg, chain, Router(), taker=TAKER)


def attempt(taker, profit, **kwargs):
    """A take of a chunk the route pays 1000 want for, earning ``profit`` want."""
    dy = 1000 * WAD
    quote = TakeQuote("route", 500 * WAD, dy, dy - int(profit * WAD), 0.0)
    lot = SimpleNamespace(token=TOKEN, want=WANT)
    kwargs.setdefault("native_price", ETH_PRICE)
    return taker.attempt(
        SimpleNamespace(address=AUCTION), lot, dry_run=False, quoted=quote, **kwargs
    )


def test_a_take_that_earns_less_than_its_gas_waits():
    # 300k gas at 5 gwei: 0.0015 ETH, $4.50 at $3000. The take earns 10 cents less.
    chain = Chain()
    taker = taker_of(chain)
    result = attempt(taker, 4.40)
    assert result.action == "waiting" and "under gas" in result.detail
    assert chain.sent == [] and len(chain.quoted) == 1
    # Far under: turned down from memory, with no request at all.
    assert attempt(taker, 0.10).action == "waiting"
    assert len(chain.quoted) == 1 and chain.sent == []
    # The curve fell: the same lot now pays for the gas and is taken.
    result = attempt(taker, 6)
    assert result.action == "taken" and "gas=" in result.detail
    ((data, kwargs),) = chain.sent
    assert kwargs["gas"] == 300_000
    cost = int(300_000 * (5 * GWEI + GWEI // 10) * ETH_PRICE)  # want wei: 18 decimals both
    # The same floor is in the transaction: a route that pays less reverts.
    assert min_profit_of(data) == pytest.approx(cost, rel=1e-9)
    assert kwargs["priority_fee"](1, 2, 3) == GWEI // 10  # the tip the cost was counted with


def test_the_tip_and_the_owners_minimum_count_too():
    chain = Chain()
    taker = taker_of(chain, min_profit=2.0)
    tip = lambda quote: lambda gas, base_fee, suggested: 5 * GWEI  # noqa: E731
    # Gas at 5 + 5 gwei is $9; with the $2 minimum a take must earn $11.
    assert attempt(taker, 10.5, priority_fee=tip).action == "waiting"
    assert attempt(taker, 11.5, priority_fee=tip).action == "taken"
    ((data, kwargs),) = chain.sent
    assert min_profit_of(data) == 11 * WAD and kwargs["priority_fee"](0, 0, 0) == 5 * GWEI


def test_without_a_native_price_the_take_goes_as_before(caplog):
    chain = Chain()
    taker = taker_of(chain)
    with caplog.at_level("WARNING"):
        assert attempt(taker, 0.10, native_price=None).action == "taken"
    assert "gas is not counted" in caplog.text
    assert min_profit_of(chain.sent[0][0]) == 0


def gas_cost(chain, tip=GWEI // 10):
    return int(chain.quote.gas * (chain.quote.base_fee + tip) * ETH_PRICE)


def refill_of(data):
    """(want swapped, least native token out, receiver) of the take's refill steps."""
    amount, min_out, receiver = decode(["uint256", "uint256", "address"], route_of(data)[3][1])
    return amount, min_out, receiver.lower()


def test_a_take_buys_its_own_gas_back_and_the_profit_stays_in_want():
    chain = Chain()
    taker = taker_of(chain)
    result = attempt(taker, 100, refill_to=KEEPER)
    cost = gas_cost(chain)
    assert result.action == "taken" and f"refill={cost}->{KEEPER}" in result.detail
    ((data, kwargs),) = chain.sent
    assert len(route_of(data)) == 4  # the lot's swap, then want -> native token
    # Exactly the gas of the take, estimated with the refill in it, goes to the sender.
    assert refill_of(data) == (cost, cost * 98 // 100, KEEPER.lower())
    assert taker.router.asked[-1] == (WANT, NATIVE_TOKEN, cost)
    assert [len(route_of(quoted)) for quoted in chain.quoted] == [2, 4]
    assert kwargs["gas"] == chain.quote.gas
    # What reaches the profit receiver is net of gas: the floor on chain is zero.
    assert min_profit_of(data) == 0
    # The next take knows what gas costs: one estimate, with the refill in place.
    attempt(taker, 100, refill_to=KEEPER)
    assert [len(route_of(quoted)) for quoted in chain.quoted[2:]] == [4]
    # Switched off, or nobody to pay it to: a plain take with the gas as its floor.
    for plain, kwargs in ((taker_of(chain, gas_refill=False), {"refill_to": KEEPER}), (taker, {})):
        attempt(plain, 100, **kwargs)
        assert len(route_of(chain.sent[-1][0])) == 2
        assert min_profit_of(chain.sent[-1][0]) == cost


def test_a_short_account_gets_more_than_the_gas():
    chain = Chain()
    taker = taker_of(chain, refill_below=0.05, refill_amount=0.01)
    assert attempt(taker, 100, refill_to=SECOND, top_up=True).action == "taken"
    data = chain.sent[-1][0]
    assert refill_of(data)[::2] == (30 * WAD, SECOND.lower())  # 0.01 ETH at $3000
    assert min_profit_of(data) == 0
    # Not short: only the gas. A small take: half its profit is no top-up, the gas still is.
    attempt(taker, 100, refill_to=SECOND)
    assert refill_of(chain.sent[-1][0])[0] == gas_cost(chain)
    attempt(taker, 5, refill_to=SECOND, top_up=True)
    assert refill_of(chain.sent[-1][0])[0] == gas_cost(chain)
    # Top-ups alone, without the buy-back.
    only = taker_of(chain, gas_refill=False, refill_below=0.05, refill_amount=0.01)
    attempt(only, 100, refill_to=SECOND)
    assert len(route_of(chain.sent[-1][0])) == 2
    attempt(only, 100, refill_to=SECOND, top_up=True)
    assert refill_of(chain.sent[-1][0])[0] == 30 * WAD


def test_the_take_never_depends_on_its_refill():
    # The refill swap reverts: the take goes without it.
    chain = Chain()
    chain.refill_reverts = True
    taker = taker_of(chain)
    assert attempt(taker, 100, refill_to=KEEPER).action == "taken"
    assert [len(route_of(data)) for data in chain.quoted] == [2, 4]
    assert len(route_of(chain.sent[0][0])) == 2

    # No route to the native token, or a route that needs more steps than are left.
    for broken in ("none", "long", "raises"):
        chain = Chain()
        taker = taker_of(chain, max_calls=4)  # a taker deployed with four steps
        swap = taker.router.swap_calls

        def best_route(token, want, amount, broken=broken):
            if want != NATIVE_TOKEN:
                return ("route", amount)
            if broken == "raises":
                raise RuntimeError("catalogue down")
            return None if broken == "none" else ("long", amount)

        taker.router.best_route = best_route
        taker.router.swap_calls = lambda route, *args, swap=swap: (
            swap(route, *args) * (2 if route == "long" else 1)
        )
        assert attempt(taker, 100, refill_to=KEEPER).action == "taken"
        assert len(route_of(chain.sent[0][0])) == 2 and min_profit_of(chain.sent[0][0]) > 0

    # The refill's own gas would eat the profit: taken without it rather than not at all.
    class Dearer(Chain):
        def gas_quote(self, to, data, *, value=0, at=None):
            quote = super().gas_quote(to, data, value=value, at=at)
            return (
                GasQuote(500_000, quote.base_fee, quote.tip) if len(route_of(data)) > 2 else quote
            )

    chain = Dearer()
    taker = taker_of(chain)
    assert attempt(taker, 6, refill_to=KEEPER).action == "taken"  # gas $4.59, with refill $7.65
    assert len(route_of(chain.sent[0][0])) == 2


def test_the_poorest_account_is_the_one_refilled():
    chain = Chain()
    chain.senders = [KEEPER, SECOND]
    balances = {KEEPER: 10**18, SECOND: 2 * 10**16}
    reads = []
    chain.balance = lambda address: reads.append(address) or balances[address]

    def keeper_of(**native):
        cfg = NetworkConfig(
            name="fake",
            rpc="fake://",
            auctions=(AUCTION,),
            auction_taker=TAKER,
            mode="keeper",
            keeper_key_env="KEY",
            native=NativeSettings(enabled=True, **native),
        )
        return NetworkKeeper(
            cfg,
            KeeperState(None),
            client=chain,
            pricer=MarketPricer([]),
            native=NativeTaker(cfg, chain, Router(), taker=TAKER),
            publishers={},
        )

    keeper = keeper_of(refill_below=0.05, refill_amount=0.02)
    reads.clear()  # the start-up report
    assert keeper.refill_target() == (SECOND, True)
    assert keeper.refill_target() == (SECOND, True) and len(reads) == 2  # read once, reused
    balances[SECOND] = 2 * 10**18
    keeper._balances_at = float("-inf")  # as after a take
    assert keeper.refill_target() == (KEEPER, False)  # the gas goes to whoever has least
    assert keeper_of(gas_refill=False).refill_target() == (None, False)
    assert keeper_of(gas_refill=False, refill_below=0.05, refill_amount=0.02).refill_target() == (
        None,
        False,
    )
    # One account and no threshold: nothing to compare, no balance read.
    chain.senders = [KEEPER]
    reads.clear()
    assert keeper_of().refill_target() == (KEEPER, False) and len(reads) == 1  # the report only


def test_refill_settings_go_together():
    raw = {"networks": {"a": {"rpc": "x", "auctions": [AUCTION], "auction_taker": TAKER}}}
    native = {"enabled": True, "min_profit": 0.5, "refill_below": 0.05, "refill_amount": 0.02}
    cfg = parse_config({**raw, "native": native}).network("a").native
    assert (cfg.min_profit, cfg.refill_below, cfg.refill_amount) == (0.5, 0.05, 0.02)
    assert cfg.gas_refill is True
    off = parse_config({**raw, "native": {"enabled": True, "gas_refill": False}}).network("a")
    assert off.native.gas_refill is False
    with pytest.raises(ValueError, match="boolean"):
        parse_config({**raw, "native": {"enabled": True, "gas_refill": "yes"}})
    with pytest.raises(ValueError, match="go together"):
        parse_config({**raw, "native": {"enabled": True, "refill_below": 0.05}})
