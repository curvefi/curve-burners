"""NetworkKeeper over the rail mechanism: one publisher per active adapter,
a rail switched off in config is skipped, one rail failing leaves the others
alone. Each rail's own publisher tests add the rail-specific cycle."""

import logging

import boa
import pytest
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.config import TokenFilter
from keepers.auction_keeper.core.keeper import NetworkKeeper
from keepers.auction_keeper.rails import AdapterPublisher
from keepers.auction_keeper.rails.cow import CowPublisher

SECOND_APP_DATA = keccak(b"CURVE_DUTCH_AUCTION_KEEPER_TEST_APP_DATA_2")


class DisabledPublisher(CowPublisher):
    """A rail whose config switch is off."""

    @classmethod
    def enabled(cls, network):
        return False


class FailingPublisher(AdapterPublisher):
    """A rail whose feed is down."""

    version = "CowAdapter"

    def publish(self, auction, lot, *, dry_run=False):
        raise RuntimeError("feed exploded")


@pytest.fixture
def second_cow_adapter(settlement, relayer, register):
    """A second adapter of the same version, so two rails share one cycle."""
    adapter = boa.load("contracts/burners/adapters/cow/CowAdapter.vy", settlement, SECOND_APP_DATA)
    register(adapter, relayer)
    return adapter


@pytest.fixture
def keeper_for(keeper_account, state, orderbook):
    def _make(config, *, publishers=None, publisher_factory=None):
        return NetworkKeeper(
            config,
            state,
            client=BoaClient(sender=keeper_account),
            publishers=publishers,
            publisher_factory=publisher_factory,
        )

    return _make


def by_adapter(outcomes):
    return {o.adapter: o.action for o in outcomes}


def test_publisher_is_enabled_by_default(network_config):
    assert AdapterPublisher.enabled(network_config()) and CowPublisher.enabled(network_config())


def test_disabled_rail_is_skipped(
    keeper_for, network_config, orderbook, sell_token, lot, cow_adapter, caplog
):
    caplog.set_level(logging.INFO)
    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    keeper = keeper_for(config, publishers={"CowAdapter": DisabledPublisher})
    assert keeper.publishers_for(keeper.auctions[0]) == []
    assert keeper.cycle() == []
    assert f"rail CowAdapter disabled in config; skipping {cow_adapter.address}" in caplog.text
    assert orderbook.posted == []
    # Logged once, not every cycle.
    keeper.cycle()
    assert caplog.text.count("rail CowAdapter disabled") == 1


def test_one_rail_failing_does_not_stop_the_others(
    keeper_for, network_config, orderbook, sell_token, lot, cow_adapter, second_cow_adapter, state
):
    def factory(cls, client, adapter, config):
        if adapter.adapter == second_cow_adapter.address:
            return FailingPublisher(client, adapter, config)
        return cls(client, adapter, config, orderbook=orderbook)

    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    keeper = keeper_for(config, publisher_factory=factory)
    assert {p.address for p in keeper.publishers_for(keeper.auctions[0])} == {
        cow_adapter.address,
        second_cow_adapter.address,
    }
    actions = by_adapter(keeper.cycle())
    assert actions == {cow_adapter.address: "posted", second_cow_adapter.address: "failed"}
    assert len(orderbook.posted) == 1
    assert list(state.orders()) == [
        f"boa:{keeper.auctions[0].address.lower()}:{cow_adapter.address.lower()}:"
        f"{sell_token.address.lower()}"
    ]
