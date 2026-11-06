"""VeloraDeltaPublisher against the real adapter, the Delta settlement mock
and the auction's signature router; the price and orders calls are faked
with the shapes of the Velora SDK (``DeltaPrice``, ``DeltaOrderToPost``,
``DeltaAuction``)."""

import logging

import boa
import pytest
from eth_abi import encode
from eth_utils import to_checksum_address

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    DELTA_ORDER_ABI,
    ERC1271_MAGIC_VALUE,
    AdapterRegistry,
    Auction,
)
from keepers.auction_keeper.config import TokenFilter, VeloraSettings
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.velora import (
    ORDERS_PATH,
    PRICES_PATH,
    DeltaOrder,
    VeloraDeltaPublisher,
    gross_up,
    pack_partner_and_fee,
)
from tests.burners.test_velora_delta_module import order_digest_reference
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

PARTNER = to_checksum_address("0x" + "fe" * 20)


@pytest.fixture(scope="module")
def delta_settlement():
    return boa.load("contracts/testing/dutch_auction/DeltaSettlementMock.vy", bytes(32))


@pytest.fixture
def velora_adapter(delta_settlement):
    return boa.load("contracts/burners/adapters/velora/VeloraDeltaAdapter.vy", delta_settlement)


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def adapter(client, auction, velora_adapter, delta_settlement, register):
    register(velora_adapter, delta_settlement)
    registry = AdapterRegistry(client, auction.registry())
    return next(a for a in registry.active_adapters() if a.adapter == velora_adapter.address)


def price_for(params, *, output: int):
    """A ``DeltaPrice`` in the SDK's shape for a same-chain sell."""
    step = {
        "input": {
            "token": {"chainId": 1, "address": params["srcToken"]},
            "amount": params["amount"],
            "amountUSD": "1",
        },
        "output": {
            "token": {"chainId": 1, "address": params["destToken"]},
            "amount": str(output),
            "amountUSD": "1",
        },
    }
    return {
        "id": "price-1",
        "side": "SELL",
        "inputToken": {"chainId": 1, "address": params["srcToken"]},
        "outputToken": {"chainId": 1, "address": params["destToken"]},
        "route": {
            "origin": step,
            "destination": step,
            "bridge": None,
            "fees": {"gas": {}, "bridge": []},
        },
        "partner": {"name": params.get("partner") or "anon", "feePercent": 0},
        "spender": "0x0000000000bbf5c5fd284e657f01bd000933c96d",
        "alternatives": [],
    }


def posted(http):
    return [c for c in http.calls if c["method"] == "POST" and c["url"].endswith(ORDERS_PATH)]


POSTED = {"id": "order-1", "status": "OPEN", "orderHash": "0x" + "ab" * 32, "order": {}}


def test_price_then_order_is_built_signed_and_posted(
    client,
    auction,
    harness,
    http,
    adapter,
    velora_adapter,
    delta_settlement,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    network_config,
):
    view = auction.lot_view(sell_token.address)
    http.on(
        "GET",
        PRICES_PATH,
        lambda params, body: price_for(params, output=view.amount_needed + 10**18),
    )
    http.on("POST", ORDERS_PATH, POSTED)
    config = network_config(order_ttl=300, velora=VeloraSettings(partner="curve"))
    publisher = VeloraDeltaPublisher(client, adapter, config, http=http)
    assert publisher.settlement == delta_settlement.address

    result = publisher.publish(auction, view)

    assert result is not None
    (price_call,) = http.requests("GET", PRICES_PATH)
    assert price_call["url"] == "https://api.paraswap.io" + PRICES_PATH
    assert price_call["params"] == {
        "srcToken": sell_token.address,
        "destToken": want.address,
        "amount": str(LOT_AMOUNT),
        "srcDecimals": 18,
        "destDecimals": 18,
        "chainId": 1,
        "side": "SELL",
        "userAddress": harness.address,
        "beneficiary": proceeds_receiver,
        "partner": "curve",
    }
    (order_call,) = posted(http)
    body = order_call["json"]
    assert set(body) == {"chainId", "order", "signature", "partiallyFillable", "type", "partner"}
    assert body["chainId"] == 1 and body["partiallyFillable"] is False and body["type"] == "LIMIT"
    order = body["order"]
    assert set(order) == {
        "owner",
        "beneficiary",
        "srcToken",
        "destToken",
        "srcAmount",
        "destAmount",
        "expectedAmount",
        "deadline",
        "kind",
        "nonce",
        "partnerAndFee",
        "permit",
        "metadata",
        "bridge",
    }
    assert order["owner"] == harness.address and order["beneficiary"] == proceeds_receiver
    assert order["srcAmount"] == str(LOT_AMOUNT) == str(result.sell_amount)
    assert order["destAmount"] == str(view.amount_needed) == str(result.buy_amount)
    assert order["expectedAmount"] == str(view.amount_needed + 10**18)  # the quote beats the floor
    assert order["deadline"] == timestamp() + 300 == result.valid_to
    assert order["kind"] == 0 and order["partnerAndFee"] == "0" and order["permit"] == "0x"
    assert order["bridge"]["destinationChainId"] == 0
    # signature = adapter ++ abi_encode(order), valid on-chain under the Order digest.
    built = DeltaOrder(
        order["owner"],
        order["beneficiary"],
        order["srcToken"],
        order["destToken"],
        int(order["srcAmount"]),
        int(order["destAmount"]),
        int(order["expectedAmount"]),
        order["deadline"],
        order["kind"],
        int(order["nonce"]),
        int(order["partnerAndFee"]),
    )
    signature = bytes.fromhex(body["signature"][2:])
    assert signature == bytes.fromhex(velora_adapter.address[2:]) + encode(
        [DELTA_ORDER_ABI], [built.as_tuple()]
    )
    digest = order_digest_reference(built.as_tuple(), publisher.domain_separator)
    assert built.digest(publisher.domain_separator) == digest
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.uid == "0x" + digest.hex()
    assert result.details["response"] == POSTED


def test_quote_below_the_curve_still_posts_the_floor(
    client, auction, http, adapter, sell_token, lot, network_config, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    http.on(
        "GET", PRICES_PATH, lambda params, body: price_for(params, output=view.amount_needed - 1)
    )
    http.on("POST", ORDERS_PATH, POSTED)
    publisher = VeloraDeltaPublisher(client, adapter, network_config(), http=http)
    result = publisher.publish(auction, view)
    assert result is not None
    order = posted(http)[0]["json"]["order"]
    assert order["destAmount"] == order["expectedAmount"] == str(view.amount_needed)
    assert "below the curve" in caplog.text


def test_fillable_order_and_partner_fee(
    client, auction, harness, http, adapter, velora_adapter, sell_token, lot, network_config
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", PRICES_PATH, lambda params, body: price_for(params, output=view.amount_needed))
    http.on("POST", ORDERS_PATH, POSTED)
    settings = VeloraSettings(partially_fillable=True, partner_address=PARTNER, partner_fee_bps=50)
    publisher = VeloraDeltaPublisher(
        client, adapter, network_config(order_ttl=300, velora=settings), http=http
    )
    result = publisher.publish(auction, view)
    assert result is not None
    body = posted(http)[0]["json"]
    assert body["partiallyFillable"] is True
    order = body["order"]
    assert int(order["partnerAndFee"]) == pack_partner_and_fee(PARTNER, 50)
    assert velora_adapter.describe(int(order["partnerAndFee"]))[:2] == (PARTNER, 50)
    dest = int(order["destAmount"])
    assert dest == gross_up(view.amount_needed, 50)
    assert dest - dest * 50 // 10_000 >= view.amount_needed > (dest - 1) - (dest - 1) * 50 // 10_000
    assert order["expectedAmount"] == order["destAmount"]
    # Validated under the FillableOrder digest.
    built = DeltaOrder(
        order["owner"],
        order["beneficiary"],
        order["srcToken"],
        order["destToken"],
        int(order["srcAmount"]),
        dest,
        int(order["expectedAmount"]),
        order["deadline"],
        0,
        int(order["nonce"]),
        int(order["partnerAndFee"]),
    )
    signature = bytes.fromhex(body["signature"][2:])
    fillable = order_digest_reference(built.as_tuple(), publisher.domain_separator, fillable=True)
    assert result.uid == "0x" + fillable.hex()
    assert harness.isValidSignature(fillable, signature) == ERC1271_MAGIC_VALUE
    assert (
        harness.isValidSignature(built.digest(publisher.domain_separator), signature)
        == ERC1271_MAGIC_VALUE
    )


def test_ttl_never_passes_the_lot_end_and_nonces_are_fresh(
    client, auction, http, adapter, sell_token, lot, network_config
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", PRICES_PATH, lambda params, body: price_for(params, output=view.amount_needed))
    http.on("POST", ORDERS_PATH, POSTED)
    publisher = VeloraDeltaPublisher(client, adapter, network_config(order_ttl=10**9), http=http)
    first = publisher.publish(auction, view)
    second = publisher.publish(auction, view)
    assert first is not None and second is not None
    assert first.valid_to == second.valid_to == view.end
    assert first.details["order"]["nonce"] != second.details["order"]["nonce"]
    assert first.uid != second.uid


def test_api_errors_are_logged_not_raised(
    client, auction, http, adapter, sell_token, lot, network_config, caplog
):
    view = auction.lot_view(sell_token.address)
    publisher = VeloraDeltaPublisher(client, adapter, network_config(), http=http)
    http.on("GET", PRICES_PATH, {"error": "No routes found with enough liquidity"})
    assert publisher.publish(auction, view) is None
    assert "No routes found" in caplog.text

    http.handlers.clear()
    http.on("GET", PRICES_PATH, lambda params, body: price_for(params, output=view.amount_needed))

    def rejected(params, json):
        raise ApiError(400, {"error": "Invalid signature"}, ORDERS_PATH)

    http.on("POST", ORDERS_PATH, rejected)
    assert publisher.publish(auction, view) is None
    assert "Invalid signature" in caplog.text


def test_dry_run_and_standing_ask(
    client, auction, harness, http, adapter, velora_adapter, sell_token, lot, network_config
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", PRICES_PATH, lambda params, body: price_for(params, output=view.amount_needed))
    publisher = VeloraDeltaPublisher(client, adapter, network_config(), http=http)
    assert publisher.publish(auction, view, dry_run=True) is not None
    assert posted(http) == []

    standing = VeloraDeltaPublisher(
        client,
        adapter,
        network_config(order_ttl=10**6, velora=VeloraSettings(post=False)),
        http=http,
    )
    http.calls.clear()
    result = standing.publish(auction, view)
    assert result is not None and http.calls == []
    expected, _ = velora_adapter.order_for(harness.address, sell_token.address, 1)
    expected = DeltaOrder.from_tuple(tuple(expected))
    order = result.details["order"]
    assert order["owner"] == expected.owner and order["beneficiary"] == expected.beneficiary
    assert (
        int(order["destAmount"])
        == int(order["expectedAmount"])
        == expected.dest_amount
        == view.amount_needed
    )
    assert order["deadline"] == expected.deadline == view.end == result.valid_to
    signature = bytes.fromhex(result.details["signature"][2:])
    assert harness.isValidSignature(bytes.fromhex(result.uid[2:]), signature) == ERC1271_MAGIC_VALUE


def test_keeper_mode_syncs_the_delta_allowance(
    harness,
    http,
    velora_adapter,
    delta_settlement,
    register,
    sell_token,
    lot,
    keeper_account,
    network_config,
):
    register(velora_adapter, delta_settlement)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == velora_adapter.address)
    view = auction.lot_view(sell_token.address)
    http.on("GET", PRICES_PATH, lambda params, body: price_for(params, output=view.amount_needed))
    http.on("POST", ORDERS_PATH, POSTED)
    publisher = VeloraDeltaPublisher(
        client, adapter, network_config(mode="keeper", keeper_key_env="KEY"), http=http
    )
    assert sell_token.allowance(harness.address, delta_settlement.address) == 0
    assert publisher.publish(auction, view) is not None
    assert sell_token.allowance(harness.address, delta_settlement.address) == 2**256 - 1


def test_gross_up_covers_the_net_floor():
    for floor in (1, 999, 10**18 + 7, 123456789):
        for bps in (0, 1, 50, 200):
            dest = gross_up(floor, bps)
            assert dest - dest * bps // 10_000 >= floor
            if bps:
                assert (dest - 1) - (dest - 1) * bps // 10_000 < floor


# Config section


def test_config_section_defaults_and_parse():
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20

    def velora_of(section=None):
        network = {"rpc": "r", "auctions": [auction]}
        if section is not None:
            network["velora"] = section
        return parse_config({"networks": {"x": network}}).network("x").velora

    velora = velora_of()
    assert velora.enabled and velora.post and velora.partner is None
    assert velora.partner_fee_bps == 0 and not velora.partially_fillable
    assert velora.order_type == "LIMIT" and velora.api_url == "https://api.paraswap.io"
    velora = velora_of(
        {
            "partner": "curve",
            "partner_address": "0x" + "fe" * 20,
            "partner_fee_bps": 50,
            "partially_fillable": True,
            "order_type": "MARKET",
        }
    )
    assert velora.partner == "curve" and velora.partner_fee_bps == 50
    assert velora.partially_fillable and velora.order_type == "MARKET"
    # A zero fee is allowed explicitly; a negative one is not.
    assert velora_of({"partner_fee_bps": 0}).partner_fee_bps == 0
    for bad in (
        {"partner_fee_bps": -1},
        {"partner": 1},
        {"nope": True},
        {"partially_fillable": "yes"},
        "polygon",
    ):
        with pytest.raises(ConfigError):
            velora_of(bad)


def test_price_source_carries_the_partner(network_config, client):
    from keepers.auction_keeper.config import PricingSettings
    from keepers.auction_keeper.pricing import build_sources

    config = network_config(
        pricing=PricingSettings(sources=("velora",)), velora=VeloraSettings(partner="curve")
    )
    (source,) = build_sources(config, client)
    assert source.name == "velora" and source.partner == "curve"


# Keeper cycle over the Velora rail


@pytest.fixture
def velora_rail(register, velora_adapter, delta_settlement):
    register(velora_adapter, delta_settlement)
    return velora_adapter.address


@pytest.fixture
def velora_feeds(http, harness, sell_token):
    """A Delta price at the live curve quote and the stored-order answer."""

    def quote_now():
        return harness.getAmountNeeded(sell_token.address, harness.available(sell_token.address))

    http.on("GET", PRICES_PATH, lambda params, body: price_for(params, output=quote_now()))
    http.on("POST", ORDERS_PATH, POSTED)
    return http


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_publishes_records_and_reposts(
    rail_keeper,
    network_config,
    velora_rail,
    velora_feeds,
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
        "VeloraDeltaAdapter",
    }

    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", velora_rail: "posted"}
    assert len(orderbook.posted) == 1 and len(posted(velora_feeds)) == 1
    record = state.order("boa", harness.address, velora_rail, sell_token.address)
    assert record is not None
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.sell_amount == harness.available(sell_token.address)
    assert record.valid_to == timestamp() + config.order_ttl

    # Within the validity both rails hold; expired, a fresh order (new nonce)
    # is built and posted at the lower curve price.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    posts = posted(velora_feeds)
    assert len(posts) == 2
    first, second = posts[0]["json"]["order"], posts[1]["json"]["order"]
    assert int(second["destAmount"]) < int(first["destAmount"])
    assert second["nonce"] != first["nonce"]


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, velora_rail, velora_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, velora=VeloraSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert velora_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, velora_rail, velora_feeds, sell_token, lot, cow_adapter
):
    def boom(params, json):
        raise RuntimeError("prices exploded")

    velora_feeds.handlers.insert(0, ("GET", PRICES_PATH, boom))
    config = network_config(**whitelisted(sell_token))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        velora_rail: "failed",
    }


def test_dry_run_posts_nothing(
    rail_keeper, network_config, velora_rail, velora_feeds, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    assert [c["method"] for c in velora_feeds.calls] == ["GET"]
