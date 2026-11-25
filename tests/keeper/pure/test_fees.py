"""The tip of a take: a share of its profit, inside a floor and a cap."""

import pytest

from keepers.auction_keeper.config import ConfigError, FeeSettings, parse_config
from keepers.auction_keeper.core.fees import GWEI, take_priority_fee
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.core.state import KeeperState
from keepers.auction_keeper.pricing import MarketPricer
from keepers.auction_keeper.rails.native import TakeQuote
from tests.keeper.pure.conftest import AUCTION, WAD

ETH = 3000.0  # want (a dollar stablecoin) per native token, for these examples
GAS = 600_000
BASE = GWEI // 5  # 0.2 gwei


def tip(settings=FeeSettings(), *, profit, price=ETH, gas=GAS, base=BASE, suggested=20_000):
    return take_priority_fee(
        settings,
        profit=int(profit * WAD),
        want_unit=WAD,
        native_price=price,
        gas=gas,
        base_fee=base,
        suggested=suggested,
    )


def test_a_wider_margin_buys_a_higher_tip():
    # $3 of profit, half of it for gas: 0.0005 ETH over 600k gas = 0.833 gwei
    # all in, of which 0.2 gwei is the base fee.
    assert tip(profit=3) == int(0.5 * 3 / ETH * 10**18 - GAS * BASE) // GAS
    assert tip(profit=3) / GWEI == pytest.approx(0.6333, abs=1e-4)
    assert tip(profit=6) > 2 * tip(profit=3)
    # The whole spend never exceeds the share of the profit.
    for profit in (0.5, 3, 10, 25):
        spent = (tip(profit=profit) + BASE) * GAS / 10**18 * ETH
        assert spent <= 0.5 * profit + 1e-9 or tip(profit=profit) == 0


def test_a_300_dollar_lot_at_a_tenth_of_a_percent_cannot_buy_a_competitive_tip():
    # $0.30 of profit. Half of it is 0.00005 ETH; the base fee alone on
    # 600k gas is 0.00012 ETH. Nothing is left for a tip: the default tips 0.
    assert tip(profit=0.30) == 0
    # A 0.84 gwei tip (what won the lot on 2026-11-24) costs $1.51 on 600k gas,
    # plus $0.36 of base fee: five to six times the take's whole profit.
    cost = (0.84 * GWEI + BASE) * GAS / 10**18 * ETH
    assert cost == pytest.approx(1.872) and cost > 6 * 0.30
    # Even with the entire profit spent on gas it stays at zero tip here.
    assert tip(FeeSettings(profit_share=1.0), profit=0.30) == 0
    # Only a floor outbids it, and a floor is paid at a loss: the owner's call.
    floor = FeeSettings(priority_floor=1.0)
    assert tip(floor, profit=0.30) == GWEI


def test_floor_and_cap_bound_the_tip():
    settings = FeeSettings(priority_floor=0.1, priority_cap=2.0)
    assert tip(settings, profit=0) == GWEI // 10
    assert tip(settings, profit=1000) == 2 * GWEI
    assert GWEI // 10 < tip(settings, profit=3) < 2 * GWEI
    # A floor above the cap (rejected by the config) would still be paid.
    assert tip(FeeSettings(priority_floor=3.0, priority_cap=2.0), profit=0) == 3 * GWEI
    # A loss-making quote (never taken) does not turn the arithmetic negative.
    assert tip(profit=-5) == 0


def test_without_a_native_price_the_node_suggestion_is_bounded():
    assert tip(profit=3, price=None, suggested=20_000) == 20_000
    assert tip(profit=3, price=None, suggested=50 * GWEI) == 5 * GWEI  # the default cap
    assert tip(FeeSettings(priority_floor=1.0), profit=3, price=None) == GWEI


def test_fees_section_parses():
    def network(**settings):
        raw = {"networks": {"eth": {"rpc": "x", "auctions": [AUCTION], **settings}}}
        return parse_config(raw).network("eth")

    assert network().fees == FeeSettings(
        priority_floor=0.0, priority_cap=5.0, profit_share=0.5, native_price=None
    )
    fees = network(
        fees={"priority_floor": 1, "priority_cap": 3, "profit_share": 0.8, "native_price": 3100}
    ).fees
    assert fees == FeeSettings(1.0, 3.0, 0.8, 3100.0)
    raw = {
        "fees": {"priority_floor": 0.5},
        "networks": {"a": {"rpc": "x", "auctions": [AUCTION], "fees": {"priority_cap": 9}}},
    }
    inherited = parse_config(raw).network("a").fees
    assert (inherited.priority_floor, inherited.priority_cap) == (0.5, 9.0)
    for bad in (
        {"priority_floor": 6},  # above the default cap
        {"profit_share": 1.5},
        {"native_price": 0},
        {"priority_cap": -1},
        {"tip": 1},
    ):
        with pytest.raises(ConfigError):
            network(fees=bad)


def test_the_keeper_sizes_a_take_tip_from_its_quote(chain):
    from tests.keeper.pure.test_cycle import config

    cfg = config(fees=FeeSettings(native_price=ETH))
    keeper = NetworkKeeper(
        cfg, KeeperState(None), client=chain, pricer=MarketPricer([]), publishers={}
    )
    book = keeper.sync(keeper.auctions[0], chain.latest_block())
    quote = TakeQuote(route=None, amount=1, dy=303 * WAD, payment=300 * WAD, impact=0.0)
    fee = keeper._priority_fee(book, quote)
    assert fee(GAS, BASE, 20_000) == tip(profit=3)
    assert keeper.native_price(book) == ETH


def test_native_price_comes_from_the_price_sources_when_not_configured(chain):
    from tests.keeper.pure.test_cycle import config

    class Feed:
        name = "feed"
        asked = 0

        def quote(self, lot):
            self.asked += 1
            assert lot.token == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"  # WETH
            return int(lot.available * ETH)

    feed = Feed()
    keeper = NetworkKeeper(
        config(),
        KeeperState(None),
        client=chain,
        pricer=MarketPricer([feed], clock=lambda: float(chain.now)),
        publishers={},
    )
    book = keeper.sync(keeper.auctions[0], chain.latest_block())
    assert keeper.native_price(book) == ETH and keeper.native_price(book) == ETH
    assert feed.asked == 1  # kept like any estimate
    # No source at all: no price, and the tip is the node's suggestion.
    blind = NetworkKeeper(
        config(), KeeperState(None), client=chain, pricer=MarketPricer([]), publishers={}
    )
    assert blind.native_price(book) is None


def test_the_client_asks_the_policy_for_the_tip_and_plain_sends_do_not():
    from types import SimpleNamespace

    from keepers.auction_keeper.chain.client import Web3Client

    client = object.__new__(Web3Client)
    client.w3 = SimpleNamespace(
        eth=SimpleNamespace(
            get_block=lambda tag: {"baseFeePerGas": BASE}, max_priority_fee=20_000, gas_price=7
        )
    )
    # An allowance sync: the node's suggestion, as before.
    assert client._fees() == {"maxPriorityFeePerGas": 20_000, "maxFeePerGas": 2 * BASE + 20_000}
    seen = []

    def policy(gas, base_fee, suggested):
        seen.append((gas, base_fee, suggested))
        return GWEI

    assert client._fees(GAS, policy) == {
        "maxPriorityFeePerGas": GWEI,
        "maxFeePerGas": 2 * BASE + GWEI,
    }
    assert seen == [(GAS, BASE, 20_000)]
    # A chain without a base fee keeps its gas price.
    client.w3.eth.get_block = lambda tag: {}
    assert client._fees(GAS, policy) == {"gasPrice": 7}
