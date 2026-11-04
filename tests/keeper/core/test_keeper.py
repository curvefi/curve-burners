"""NetworkKeeper: discovery, filters, and the repost policy over the TTL."""

import boa
import pytest

from keepers.auction_keeper.chain.abi import address_word
from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.client import Log
from keepers.auction_keeper.chain.contracts import LOT_STAGED_TOPIC, Auction, LotView
from keepers.auction_keeper.config import TokenFilter
from keepers.auction_keeper.core.keeper import NetworkKeeper, needs_publish
from keepers.auction_keeper.core.state import KeeperState, OrderRecord
from keepers.auction_keeper.pricing import MarketQuote
from tests.keeper.conftest import DOMAIN_SEPARATOR, FakeOrderbook


def factory(orderbook):
    def _make(cls, client, adapter, config):
        return cls(client, adapter, config, orderbook=orderbook)

    return _make


@pytest.fixture
def client(keeper_account):
    return BoaClient(sender=keeper_account)


@pytest.fixture
def keeper_for(client, state, orderbook):
    def _make(config, *, dry_run=False, state_=None, **kwargs):
        return NetworkKeeper(
            config,
            state_ or state,
            client=client,
            dry_run=dry_run,
            publisher_factory=factory(orderbook),
            **kwargs,
        )

    return _make


class FakePricer:
    """Market per token (want for the whole lot); None or missing: no market."""

    def __init__(self, markets):
        self.markets = {token.lower(): amount for token, amount in markets.items()}

    def market(self, lot):
        amount = self.markets.get(lot.token.lower())
        return MarketQuote(amount, "fake", 0.0) if amount else None


def actions(outcomes):
    return [(o.token, o.action) for o in outcomes]


def test_whitelisted_live_lot_is_posted_then_kept(
    keeper_for, network_config, orderbook, sell_token, harness, lot, state
):
    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    keeper = keeper_for(config)

    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    assert len(orderbook.posted) == 1
    record = state.order(
        "boa",
        harness.address,
        keeper.publishers_for(keeper.auctions[0])[0].address,
        sell_token.address,
    )
    assert record is not None and record.lot_start == harness.window(sell_token.address)[0]

    # Within the TTL nothing is reposted; the loop sleeps the poll interval.
    assert actions(keeper.cycle()) == [(sell_token.address, "kept")]
    assert len(orderbook.posted) == 1
    assert keeper.wait_seconds(0) == config.poll_interval

    # One second before expiry: still kept, and the loop wakes for the expiry.
    boa.env.time_travel(seconds=config.order_ttl - 1)
    assert actions(keeper.cycle()) == [(sell_token.address, "kept")]
    assert keeper.wait_seconds(0) == 1
    assert keeper.wait_seconds(0.5) == 1  # never below a second once something is due

    # Expired: reposted at the fresh quote, never two open orders for one lot.
    boa.env.time_travel(seconds=1)
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    assert len(orderbook.posted) == 2
    assert int(orderbook.posted[1]["buyAmount"]) < int(orderbook.posted[0]["buyAmount"])


def test_offline_mode_sends_nothing_keeper_mode_syncs_allowances(
    keeper_for, client, network_config, orderbook, sell_token, harness, relayer, lot
):
    whitelist = TokenFilter(whitelist=frozenset([sell_token.address]))
    offline = keeper_for(network_config(tokens=whitelist))
    assert actions(offline.cycle()) == [(sell_token.address, "posted")]
    assert client.sent == []
    assert sell_token.allowance(harness.address, relayer) == 0

    config = network_config(tokens=whitelist, mode="keeper", keeper_key_env="KEY")
    keeper = keeper_for(config, state_=KeeperState(None))
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    assert len(client.sent) == 1 and client.sent[0][0] == harness.address
    assert sell_token.allowance(harness.address, relayer) == 2**256 - 1


def test_keeper_mode_requires_a_sender(state, network_config, orderbook):
    with pytest.raises(RuntimeError, match="mode keeper"):
        NetworkKeeper(
            network_config(mode="keeper", keeper_key_env="KEY"),
            state,
            client=BoaClient(),
            publisher_factory=factory(orderbook),
        )


def test_blacklist_and_inactive_lots(
    keeper_for, network_config, orderbook, sell_token, other_token, stage, lot
):
    stage(other_token)
    config = network_config(
        tokens=TokenFilter(
            whitelist=frozenset([sell_token.address, other_token.address]),
            blacklist=frozenset([other_token.address]),
        )
    )
    assert actions(keeper_for(config).cycle()) == [(sell_token.address, "posted")]

    config = network_config(
        tokens=TokenFilter(whitelist=frozenset([sell_token.address, other_token.address]))
    )
    outcomes = keeper_for(config).cycle()
    assert sorted(actions(outcomes)) == sorted(
        [(sell_token.address, "kept"), (other_token.address, "posted")]
    )


def test_tokens_discovered_from_lot_staged_logs(
    keeper_for, client, network_config, orderbook, sell_token, harness, lot, state
):
    client.logs.append(
        Log(
            address=harness.address,
            topics=(LOT_STAGED_TOPIC, address_word(sell_token.address)),
            data=b"",
            block_number=5,
        )
    )
    config = network_config(start_block=1, log_chunk=2)
    keeper = keeper_for(config)
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    assert state.known_tokens("boa", harness.address) == {sell_token.address}
    assert state.scan_cursor("boa", harness.address) == client.latest_block().number

    # The scan cursor moves on; the token stays known without rescanning.
    client.logs.clear()
    assert actions(keeper.cycle()) == [(sell_token.address, "kept")]


def test_without_start_block_only_future_lots_are_discovered(
    keeper_for, client, network_config, sell_token, harness, lot
):
    client.logs.append(
        Log(
            address=harness.address,
            topics=(LOT_STAGED_TOPIC, address_word(sell_token.address)),
            data=b"",
            block_number=client.latest_block().number - 1,
        )
    )
    keeper = keeper_for(network_config())
    assert keeper.cycle() == []


def test_restaged_lot_is_reposted(
    keeper_for, network_config, orderbook, sell_token, harness, stage, lot
):
    config = network_config(
        tokens=TokenFilter(whitelist=frozenset([sell_token.address])), order_ttl=10**6
    )
    keeper = keeper_for(config)
    keeper.cycle()
    assert actions(keeper.cycle()) == [(sell_token.address, "kept")]
    # Next week's frame: a fresh window and curve.
    boa.env.time_travel(seconds=7 * 24 * 3600)
    stage(sell_token)
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    assert len(orderbook.posted) == 2


def test_unknown_adapter_version_is_skipped(
    keeper_for, network_config, orderbook, sell_token, registry, owner, lot, caplog
):
    unknown = boa.load("contracts/testing/dutch_auction/AdapterMock.vy")
    with boa.env.prank(owner):
        registry.set_adapter(unknown, boa.env.generate_address("executor"))
        registry.activate_adapter(unknown)
    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    outcomes = keeper_for(config).cycle()
    assert actions(outcomes) == [(sell_token.address, "posted")]
    assert "no publisher for adapter" in caplog.text


def test_dry_run_records_nothing(
    keeper_for, network_config, orderbook, sell_token, harness, lot, state
):
    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    keeper = keeper_for(config, dry_run=True)
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    assert orderbook.posted == [] and state.orders() == {}


def test_state_round_trips_through_disk(tmp_path):
    path = tmp_path / "state.json"
    state = KeeperState(path)
    record = OrderRecord(
        uid="0xabc", valid_to=10, sell_amount=1, buy_amount=2, posted_at=3, lot_start=4
    )
    state.set_order("net", "0xA", "0xB", "0xC", record)
    state.record_scan("net", "0xA", 99, {"0xC"})
    reloaded = KeeperState(path)
    assert reloaded.order("net", "0xa", "0xb", "0xc") == record
    assert reloaded.scan_cursor("net", "0xA") == 99 and reloaded.known_tokens("net", "0xA") == {
        "0xC"
    }


def test_needs_publish_policy():
    lot = LotView(
        "0xA",
        "0xT",
        "0xW",
        "0xR",
        timestamp=1000,
        start=900,
        end=2000,
        initial_amount=5,
        available=5,
        amount_needed=1,
    )
    assert needs_publish(None, lot)
    fresh = OrderRecord(
        "0x1", valid_to=1100, sell_amount=5, buy_amount=1, posted_at=990, lot_start=900
    )
    assert not needs_publish(fresh, lot)
    assert needs_publish(OrderRecord(**{**fresh.__dict__, "valid_to": 1000}), lot)  # expired
    assert needs_publish(OrderRecord(**{**fresh.__dict__, "lot_start": 100}), lot)  # restaged


def test_run_loop_stops_on_wait(keeper_for, network_config, sell_token, lot):
    keeper = keeper_for(
        network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    )
    waits = []

    def wait(seconds):
        waits.append(seconds)
        return len(waits) == 2

    keeper.run(wait=wait)
    assert len(waits) == 2 and all(0 <= w <= 30 for w in waits)


def test_fake_orderbook_uid_matches_publisher(orderbook):
    assert isinstance(orderbook, FakeOrderbook) and orderbook.domain_separator == DOMAIN_SEPARATOR


def test_order_validity_follows_the_schedule(
    keeper_for, client, network_config, orderbook, sell_token, harness, lot
):
    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    auction = Auction(client, harness.address)
    needed = auction.lot_view(sell_token.address).amount_needed

    # Inside the band: the minimum validity, reposted as it expires.
    keeper = keeper_for(config, pricer=FakePricer({sell_token.address: needed}))
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    now = auction.lot_view(sell_token.address).timestamp
    assert orderbook.posted[-1]["validTo"] == now + config.ttl.min_ttl
    plan = keeper.plan_for(auction, auction.lot_view(sell_token.address))
    assert plan.zone == "near" and plan.market == needed

    # Far above the market: one order until the curve is due to enter the band.
    keeper = keeper_for(
        config, pricer=FakePricer({sell_token.address: needed // 20}), state_=KeeperState(None)
    )
    assert actions(keeper.cycle()) == [(sell_token.address, "posted")]
    plan = keeper.plan_for(auction, auction.lot_view(sell_token.address))
    assert plan.zone == "far" and plan.band_entry is not None
    # Hours away: the validity is the schedule's cap, well past the uniform TTL.
    assert orderbook.posted[-1]["validTo"] == plan.valid_to == now + config.ttl.max_ttl
    assert plan.band_entry > plan.valid_to > now + config.order_ttl
    assert keeper.wait_seconds(0) == config.poll_interval


def test_open_order_budget_serves_lots_nearest_the_market_first(
    keeper_for, client, network_config, orderbook, erc20_deployer, stage, harness, state
):
    tokens = [erc20_deployer.deploy(f"Token {i}", f"T{i}", 18) for i in range(3)]
    for token in tokens:
        stage(token)
    auction = Auction(client, harness.address)
    needed = auction.lot_view(tokens[0].address).amount_needed
    near, far, farther = (t.address for t in tokens)
    pricer = FakePricer({near: needed, far: needed // 20, farther: needed // 2000})
    config = network_config(
        tokens=TokenFilter(whitelist=frozenset([near, far, farther])), max_open_orders=2
    )
    keeper = keeper_for(config, pricer=pricer)

    outcomes = {o.token: o for o in keeper.cycle()}
    assert outcomes[near].action == "posted" and outcomes[far].action == "posted"
    assert outcomes[farther].action == "skipped" and "budget" in outcomes[farther].detail
    assert len(orderbook.posted) == 2

    # The near order expires first and takes the freed slot again; the
    # farthest lot keeps waiting behind the long far order.
    boa.env.time_travel(seconds=config.ttl.min_ttl)
    outcomes = {o.token: o for o in keeper.cycle()}
    assert outcomes[near].action == "posted" and outcomes[far].action == "kept"
    assert outcomes[farther].action == "skipped"
    assert len(state.open_orders("boa", harness.address, orderbook_adapter(keeper), 0)) == 2


def orderbook_adapter(keeper):
    return keeper.publishers_for(keeper.auctions[0])[0].address
