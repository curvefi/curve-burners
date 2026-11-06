"""UniswapXPublisher against the real adapter, the reactor/Permit2 mocks and
the auction's signature router; the order-service POST is faked."""

import logging

import boa
import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    ERC1271_MAGIC_VALUE,
    UNISWAPX_ORDER_ABI,
    AdapterRegistry,
    Auction,
)
from keepers.auction_keeper.config import TokenFilter, UniswapXSettings
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.uniswapx import ExclusiveDutchOrder, UniswapXPublisher
from tests.burners.adapters.uniswapx.test_module import permit_digest_reference
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

PERMIT2_DOMAIN = keccak(b"PERMIT2_KEEPER_TEST_DOMAIN")


@pytest.fixture(scope="module")
def permit2():
    return boa.load("contracts/testing/dutch_auction/Permit2Mock.vy", PERMIT2_DOMAIN)


@pytest.fixture(scope="module")
def reactor(permit2):
    return boa.load("contracts/testing/dutch_auction/ReactorMock.vy", permit2)


@pytest.fixture
def uniswapx_adapter(reactor):
    return boa.load("contracts/burners/adapters/uniswapx/UniswapXAdapter.vy", reactor)


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def publisher_for(client, auction, uniswapx_adapter, permit2, register, http):
    register(uniswapx_adapter, permit2)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == uniswapx_adapter.address)

    def _make(config, client_=None, auction_=None):
        return UniswapXPublisher(client_ or client, adapter, config, http=http)

    return _make


def test_order_matches_order_for_and_validates(
    publisher_for,
    auction,
    harness,
    network_config,
    uniswapx_adapter,
    reactor,
    permit2,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    http,
):
    publisher = publisher_for(network_config(order_ttl=10**6))
    assert publisher.reactor == reactor.address and publisher.permit2 == permit2.address
    assert publisher.domain_separator == PERMIT2_DOMAIN
    view = auction.lot_view(sell_token.address)

    result = publisher.publish(auction, view)

    assert result is not None and http.calls == []  # post defaults to False
    expected, _ = uniswapx_adapter.order_for(harness.address, sell_token.address, 1)
    expected = ExclusiveDutchOrder.from_tuple(tuple(expected))
    encoded = bytes.fromhex(result.details["encodedOrder"][2:])
    order = ExclusiveDutchOrder.from_tuple(tuple(decode([UNISWAPX_ORDER_ABI], encoded)[0]))
    assert order.info.reactor == reactor.address and order.info.swapper == harness.address
    assert order.info.deadline == view.end == result.valid_to  # the TTL is longer than the lot
    assert order.input_token == sell_token.address
    assert order.input_start_amount == order.input_end_amount == LOT_AMOUNT == result.sell_amount
    (output,) = order.outputs
    assert output.token == want.address and output.recipient == proceeds_receiver
    assert output.start_amount == output.end_amount == view.amount_needed == result.buy_amount
    assert order.decay_start_time == order.decay_end_time == timestamp()
    assert order.exclusive_filler == "0x0000000000000000000000000000000000000000"
    # Same as the on-chain template but for the nonce.
    assert expected.with_deadline(order.info.deadline).as_tuple()[1:] == order.as_tuple()[1:]
    # The signature validates on-chain for the Permit2 witness digest.
    signature = bytes.fromhex(result.details["signature"][2:])
    assert signature == bytes.fromhex(uniswapx_adapter.address[2:]) + encode(
        [UNISWAPX_ORDER_ABI], [order.as_tuple()]
    )
    digest = permit_digest_reference(order.as_tuple(), PERMIT2_DOMAIN)
    assert order.permit_digest(PERMIT2_DOMAIN) == digest
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.uid == "0x" + order.hash().hex()


def test_ttl_shortens_the_deadline_and_fresh_nonces(
    publisher_for, auction, network_config, sell_token, lot
):
    publisher = publisher_for(network_config(order_ttl=300))
    view = auction.lot_view(sell_token.address)
    first = publisher.publish(auction, view)
    second = publisher.publish(auction, view)
    assert first is not None and second is not None
    assert first.valid_to == second.valid_to == timestamp() + 300
    assert first.details["deadline"] == timestamp() + 300
    assert first.details["nonce"] != second.details["nonce"]
    assert first.uid != second.uid


def test_post_attempt_reports_the_order_service_rejection(
    publisher_for, auction, client, network_config, sell_token, lot, http, caplog
):
    caplog.set_level(logging.INFO)

    def reject(params, json):
        raise ApiError(400, {"detail": "signature must be 65 bytes"}, "order-service")

    http.on("POST", "api.uniswap.org/v2/order", reject)
    publisher = publisher_for(network_config(uniswapx=UniswapXSettings(post=True)))
    result = publisher.publish(auction, auction.lot_view(sell_token.address))
    assert result is not None
    (call,) = http.calls
    assert call["url"] == "https://api.uniswap.org/v2/order"
    assert set(call["json"]) == {"encodedOrder", "signature", "chainId"}
    assert call["json"]["chainId"] == client.chain_id
    assert call["json"]["encodedOrder"] == result.details["encodedOrder"]
    assert result.details["orderService"] == {
        "accepted": False,
        "status": 400,
        "body": {"detail": "signature must be 65 bytes"},
    }
    assert "rejected the order (expected)" in caplog.text

    http.handlers.clear()
    http.on("POST", "api.uniswap.org/v2/order", {"hash": "0xabc"})
    result = publisher.publish(auction, auction.lot_view(sell_token.address))
    assert result is not None and result.details["orderService"] == {
        "accepted": True,
        "body": {"hash": "0xabc"},
    }


def test_dry_run_and_inactive_lot(
    publisher_for, auction, network_config, sell_token, other_token, lot, http
):
    publisher = publisher_for(network_config(uniswapx=UniswapXSettings(post=True)))
    result = publisher.publish(auction, auction.lot_view(sell_token.address), dry_run=True)
    assert result is not None and http.calls == []
    assert publisher.publish(auction, auction.lot_view(other_token.address)) is None


def test_keeper_mode_syncs_the_permit2_allowance(
    harness,
    network_config,
    uniswapx_adapter,
    permit2,
    register,
    sell_token,
    lot,
    keeper_account,
    http,
):
    register(uniswapx_adapter, permit2)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == uniswapx_adapter.address)
    config = network_config(mode="keeper", keeper_key_env="KEY")
    publisher = UniswapXPublisher(client, adapter, config, http=http)
    assert sell_token.allowance(harness.address, permit2.address) == 0
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is not None
    assert sell_token.allowance(harness.address, permit2.address) == 2**256 - 1
    assert len(client.sent) == 1


def test_underpriced_order_is_rejected_with_reason(
    publisher_for, auction, harness, network_config, uniswapx_adapter, sell_token, lot
):
    publisher = publisher_for(network_config())
    built, _ = uniswapx_adapter.order_for(harness.address, sell_token.address, 5)
    order = ExclusiveDutchOrder.from_tuple(tuple(built))
    (output,) = order.outputs
    cheap = ExclusiveDutchOrder(
        **{
            **order.__dict__,
            "outputs": (
                output.__class__(
                    output.token, output.start_amount - 1, output.end_amount - 1, output.recipient
                ),
            ),
        }
    )
    reason = publisher.validate_signature(
        auction, cheap.permit_digest(PERMIT2_DOMAIN), cheap.signature(uniswapx_adapter.address)
    )
    assert reason == "BadBuyAmount()"


# Config section


def test_config_section_defaults_and_parse():
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    network = parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x")
    assert network.uniswapx.enabled and not network.uniswapx.post
    assert network.uniswapx.order_service_url == "https://api.uniswap.org/v2/order"
    assert network.uniswapx.api_key is None
    section = {"enabled": False, "post": True, "order_service_url": "https://private/order"}
    network = parse_config(
        {"networks": {"x": {"rpc": "r", "auctions": [auction], "uniswapx": section}}}
    ).network("x")
    assert not network.uniswapx.enabled and network.uniswapx.post
    assert network.uniswapx.order_service_url == "https://private/order"
    for bad in (
        {"uniswapx": {"enabled": 1}},
        {"uniswapx": {"nope": True}},
        {"uniswapx": {"order_service_url": 5}},
        {"uniswapx": "polygon"},
    ):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], **bad}}})


# Keeper cycle over the UniswapX rail


@pytest.fixture
def uniswapx_rail(register, uniswapx_adapter, permit2):
    register(uniswapx_adapter, permit2)
    return uniswapx_adapter.address


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_publishes_records_and_reposts(
    rail_keeper,
    network_config,
    uniswapx_rail,
    http,
    orderbook,
    sell_token,
    harness,
    lot,
    state,
    cow_adapter,
):
    config = network_config(**whitelisted(sell_token, poll_interval=60, order_ttl=300))
    keeper = rail_keeper(config)
    assert {p.version for p in keeper.publishers_for(keeper.auctions[0])} == {
        "CowAdapter",
        "UniswapXAdapter",
    }

    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", uniswapx_rail: "posted"}
    assert len(orderbook.posted) == 1 and http.calls == []  # build + validate + log only
    record = state.order("boa", harness.address, uniswapx_rail, sell_token.address)
    assert record is not None
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.sell_amount == harness.available(sell_token.address)
    assert record.valid_to == timestamp() + config.order_ttl

    # Within the validity the order holds; expired, it is reposted at the
    # fresh quote under a fresh nonce (a new order hash).
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    reposted = state.order("boa", harness.address, uniswapx_rail, sell_token.address)
    assert reposted is not None and reposted.uid != record.uid
    assert reposted.buy_amount < record.buy_amount


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, uniswapx_rail, http, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, uniswapx=UniswapXSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert http.calls == []


def test_dry_run_posts_nothing(
    rail_keeper, network_config, uniswapx_rail, http, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token, uniswapx=UniswapXSettings(post=True)))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {} and http.calls == []
