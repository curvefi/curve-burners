"""KyberDsloPublisher against the real adapter, the DSLO protocol mock and
the auction's signature router; the maker API's sign-message and orders
calls are faked with the shapes of docs.kyberswap.com (Limit Order API >
Maker API: request fields and the EIP-712 message)."""

import logging

import boa
import pytest
from eth_abi import encode

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    DSLO_ORDER_ABI,
    ERC1271_MAGIC_VALUE,
    AdapterRegistry,
    Auction,
)
from keepers.auction_keeper.config import KyberSettings, TokenFilter
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.kyber import (
    ORDERS_PATH,
    SIGN_MESSAGE_PATH,
    DsloOrder,
    KyberDsloPublisher,
    linear_getters,
    timestamp_below,
)
from tests.burners.test_kyber_module import api_predicate, order_digest_reference, pack_fee_config
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

FEE_RECIPIENT = "0x" + "fe" * 20


@pytest.fixture(scope="module")
def dslo_protocol():
    return boa.load("contracts/testing/dutch_auction/DsloProtocolMock.vy", bytes(32))


@pytest.fixture
def kyber_adapter(dslo_protocol):
    return boa.load("contracts/burners/adapters/kyber/KyberDsloAdapter.vy", dslo_protocol)


SALT = 0x0102030405060708091011


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def adapter(client, auction, kyber_adapter, dslo_protocol, register):
    register(kyber_adapter, dslo_protocol)
    registry = AdapterRegistry(client, auction.registry())
    return next(a for a in registry.active_adapters() if a.adapter == kyber_adapter.address)


@pytest.fixture
def sign_message_for(dslo_protocol, client):
    """The sign-message answer the maker API gives for a request body: the
    EIP-712 domain of the protocol and the message with the API's salt,
    feeConfig (a 0.1% taker-asset fee), linear getters and the
    and([nonceEquals(maker, 0), timestampBelow(expiredAt)]) predicate."""

    def _answer(params, body, **overrides):
        making, taking = int(body["makingAmount"]), int(body["takingAmount"])
        getters = linear_getters(making, taking)
        message = {
            "salt": str(SALT),
            "makerAsset": body["makerAsset"],
            "takerAsset": body["takerAsset"],
            "maker": body["maker"],
            "receiver": body.get("receiver") or body["maker"],
            "allowedSender": "0x0000000000000000000000000000000000000000",
            "makingAmount": body["makingAmount"],
            "takingAmount": body["takingAmount"],
            "feeConfig": str(pack_fee_config(FEE_RECIPIENT, 100, True)),
            "makerAssetData": "0x",
            "takerAssetData": "0x",
            "getMakerAmount": "0x" + getters[0].hex(),
            "getTakerAmount": "0x" + getters[1].hex(),
            "predicate": "0x"
            + api_predicate(dslo_protocol.address, body["maker"], 0, body["expiredAt"]).hex(),
            "interaction": "0x",
        }
        message.update(overrides.pop("message", {}))
        domain = {
            "name": "Kyber DSLO Protocol",
            "version": "1",
            "chainId": client.chain_id,
            "verifyingContract": overrides.pop("verifying_contract", dslo_protocol.address),
        }
        return {
            "code": 0,
            "message": "successfully",
            "data": {"domain": domain, "types": {}, "primaryType": "Order", "message": message},
        }

    return _answer


CREATED = {"code": 0, "message": "successfully", "data": {"id": 1234}}


def orders_posted(http):
    """The orders POSTs (the sign-message path contains the orders path)."""
    return [c for c in http.calls if c["method"] == "POST" and c["url"].endswith(ORDERS_PATH)]


def test_sign_message_order_is_checked_signed_and_posted(
    client,
    auction,
    harness,
    http,
    adapter,
    kyber_adapter,
    dslo_protocol,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    sign_message_for,
    network_config,
):
    view = auction.lot_view(sell_token.address)
    http.on("POST", SIGN_MESSAGE_PATH, sign_message_for)
    http.on("POST", ORDERS_PATH, CREATED)
    config = network_config(order_ttl=300, kyber=KyberSettings(client_id="curve"))
    publisher = KyberDsloPublisher(client, adapter, config, http=http)
    assert publisher.protocol == dslo_protocol.address

    result = publisher.publish(auction, view)

    assert result is not None
    (sign_call,) = http.requests("POST", SIGN_MESSAGE_PATH)
    assert sign_call["url"] == "https://limit-order.kyberswap.com" + SIGN_MESSAGE_PATH
    assert sign_call["headers"] == {"X-Client-Id": "curve"}
    assert sign_call["json"] == {
        "chainId": "1",
        "makerAsset": sell_token.address,
        "takerAsset": want.address,
        "maker": harness.address,
        "receiver": proceeds_receiver,
        "makingAmount": str(LOT_AMOUNT),
        "takingAmount": str(view.amount_needed),
        "expiredAt": timestamp() + 300,
    }
    (order_call,) = orders_posted(http)
    assert order_call["url"].endswith(ORDERS_PATH) and order_call["headers"] == {
        "X-Client-Id": "curve"
    }
    body = order_call["json"]
    assert body == {
        **sign_call["json"],
        "salt": str(SALT),
        "signature": result.details["signature"],
    }
    # signature = adapter ++ abi_encode(message order), valid on-chain for the protocol's digest.
    order = DsloOrder.from_message(sign_message_for(None, sign_call["json"])["data"]["message"])
    signature = bytes.fromhex(body["signature"][2:])
    assert signature == bytes.fromhex(kyber_adapter.address[2:]) + encode(
        [DSLO_ORDER_ABI], [order.as_tuple()]
    )
    digest = order_digest_reference(order.as_tuple(), publisher.domain_separator)
    assert order.digest(publisher.domain_separator) == digest
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.uid == "0x" + digest.hex()
    # timestampBelow(expiredAt) is strict: the last fillable second is expiredAt - 1.
    assert result.valid_to == timestamp() + 299 == kyber_adapter.describe(order.as_tuple())[4]
    assert result.sell_amount == LOT_AMOUNT and result.buy_amount == view.amount_needed
    assert result.details["response"] == {"id": 1234}
    assert result.details["order"]["feeConfig"] == str(pack_fee_config(FEE_RECIPIENT, 100, True))


def test_ttl_never_passes_the_lot_end(
    client, auction, http, adapter, sell_token, lot, sign_message_for, network_config
):
    view = auction.lot_view(sell_token.address)
    http.on("POST", SIGN_MESSAGE_PATH, sign_message_for)
    http.on("POST", ORDERS_PATH, CREATED)
    publisher = KyberDsloPublisher(client, adapter, network_config(order_ttl=10**9), http=http)
    result = publisher.publish(auction, view)
    assert result is not None and result.valid_to == view.end - 1
    assert http.requests("POST", SIGN_MESSAGE_PATH)[0]["json"]["expiredAt"] == view.end


def test_messages_that_do_not_match_the_lot_are_skipped(
    client,
    auction,
    http,
    adapter,
    sell_token,
    other_token,
    lot,
    sign_message_for,
    network_config,
    caplog,
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = KyberDsloPublisher(client, adapter, network_config(order_ttl=300), http=http)
    cases = [
        (dict(verifying_contract="0x" + "12" * 20), "is not the adapter's protocol"),
        (dict(message={"maker": "0x" + "11" * 20}), "is not the auction"),
        (dict(message={"receiver": "0x" + "11" * 20}), "is not the auction's receiver"),
        (dict(message={"makerAsset": other_token.address}), "are not the lot pair"),
        (dict(message={"takingAmount": str(view.amount_needed - 1)}), "differ from the lot"),
        (
            dict(message={"predicate": "0x" + timestamp_below(timestamp() + 500).hex()}),
            "is past the requested",
        ),
        # Shape failures the predicate check does not cover surface at the router.
        (dict(message={"interaction": "0x" + "ab" * 20}), "OrderNotValid('BadInteraction')"),
    ]
    for overrides, reason in cases:
        http.handlers.clear()
        http.on("POST", SIGN_MESSAGE_PATH, lambda p, b, o=overrides: sign_message_for(p, b, **o))
        assert publisher.publish(auction, view) is None, reason
        assert reason in caplog.text
    assert orders_posted(http) == []


def test_underpriced_message_is_rejected_by_the_router(
    client,
    auction,
    harness,
    http,
    adapter,
    kyber_adapter,
    sell_token,
    lot,
    sign_message_for,
    network_config,
):
    view = auction.lot_view(sell_token.address)
    body = {
        "makingAmount": str(LOT_AMOUNT),
        "takingAmount": str(view.amount_needed - 1),
        "makerAsset": sell_token.address,
        "takerAsset": view.want,
        "maker": harness.address,
        "receiver": view.receiver,
        "expiredAt": timestamp() + 300,
    }
    order = DsloOrder.from_message(sign_message_for(None, body)["data"]["message"])
    publisher = KyberDsloPublisher(client, adapter, network_config(), http=http)
    reason = publisher.validate_signature(
        auction, order.digest(publisher.domain_separator), order.signature(kyber_adapter.address)
    )
    assert reason == "BadBuyAmount()"


def test_api_errors_are_logged_not_raised(
    client, auction, http, adapter, sell_token, lot, sign_message_for, network_config, caplog
):
    view = auction.lot_view(sell_token.address)
    publisher = KyberDsloPublisher(client, adapter, network_config(), http=http)
    http.on("POST", SIGN_MESSAGE_PATH, {"code": 4000, "message": "invalid maker", "data": None})
    assert publisher.publish(auction, view) is None
    assert "invalid maker" in caplog.text

    http.handlers.clear()
    http.on("POST", SIGN_MESSAGE_PATH, sign_message_for)

    def rejected(params, json):
        raise ApiError(400, {"message": "signature invalid"}, ORDERS_PATH)

    http.on("POST", ORDERS_PATH, rejected)
    assert publisher.publish(auction, view) is None
    assert "signature invalid" in caplog.text


def test_dry_run_signs_but_posts_nothing(
    client, auction, http, adapter, sell_token, lot, sign_message_for, network_config
):
    view = auction.lot_view(sell_token.address)
    http.on("POST", SIGN_MESSAGE_PATH, sign_message_for)
    publisher = KyberDsloPublisher(client, adapter, network_config(), http=http)
    assert publisher.publish(auction, view, dry_run=True) is not None
    assert orders_posted(http) == []


def test_standing_ask_matches_order_for_when_not_posting(
    client, auction, harness, http, adapter, kyber_adapter, sell_token, lot, network_config
):
    view = auction.lot_view(sell_token.address)
    publisher = KyberDsloPublisher(
        client, adapter, network_config(order_ttl=10**6, kyber=KyberSettings(post=False)), http=http
    )
    result = publisher.publish(auction, view)
    assert result is not None and http.calls == []
    expected, _ = kyber_adapter.order_for(harness.address, sell_token.address, 1)
    expected = DsloOrder.from_tuple(tuple(expected))
    order = result.details["order"]
    assert order["maker"] == expected.maker == harness.address
    assert order["receiver"] == expected.receiver and order["feeConfig"] == "0"
    assert order["makingAmount"] == str(LOT_AMOUNT) and order["takingAmount"] == str(
        view.amount_needed
    )
    assert order["getMakerAmount"] == "0x" + expected.get_maker_amount.hex()
    assert order["predicate"] == "0x" + expected.predicate.hex()  # timestampBelow(lot end + 1)
    assert result.valid_to == view.end
    signature = bytes.fromhex(result.details["signature"][2:])
    assert harness.isValidSignature(bytes.fromhex(result.uid[2:]), signature) == ERC1271_MAGIC_VALUE

    short = KyberDsloPublisher(
        client, adapter, network_config(order_ttl=120, kyber=KyberSettings(post=False)), http=http
    )
    result = short.publish(auction, view)
    assert result is not None and result.valid_to == timestamp() + 120
    assert result.details["order"]["predicate"] == "0x" + timestamp_below(timestamp() + 121).hex()


def test_keeper_mode_syncs_the_protocol_allowance(
    harness,
    http,
    kyber_adapter,
    dslo_protocol,
    register,
    sell_token,
    lot,
    keeper_account,
    sign_message_for,
    network_config,
):
    register(kyber_adapter, dslo_protocol)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == kyber_adapter.address)
    http.on("POST", SIGN_MESSAGE_PATH, sign_message_for)
    http.on("POST", ORDERS_PATH, CREATED)
    publisher = KyberDsloPublisher(
        client, adapter, network_config(mode="keeper", keeper_key_env="KEY"), http=http
    )
    assert sell_token.allowance(harness.address, dslo_protocol.address) == 0
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is not None
    assert sell_token.allowance(harness.address, dslo_protocol.address) == 2**256 - 1


# Config section


def test_config_section_defaults_and_parse():
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    kyber = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x").kyber
    )
    assert kyber.enabled and kyber.post and kyber.client_id is None and kyber.api_key is None
    assert kyber.api_url == "https://limit-order.kyberswap.com"
    section = {"client_id": "curve", "post": False}
    kyber = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], "kyber": section}}})
        .network("x")
        .kyber
    )
    assert kyber.client_id == "curve" and not kyber.post
    for bad in (
        {"kyber": {"client_id": 1}},
        {"kyber": {"nope": True}},
        {"kyber": {"enabled": "yes"}},
        {"kyber": "polygon"},
    ):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], **bad}}})


def test_price_source_carries_the_client_id(network_config, client):
    from keepers.auction_keeper.config import PricingSettings
    from keepers.auction_keeper.pricing import build_sources

    config = network_config(
        pricing=PricingSettings(sources=("kyber",)), kyber=KyberSettings(client_id="curve")
    )
    (source,) = build_sources(config, client)
    assert source.name == "kyber" and source.client_id == "curve"


# Keeper cycle over the Kyber rail


@pytest.fixture
def kyber_rail(register, kyber_adapter, dslo_protocol):
    register(kyber_adapter, dslo_protocol)
    return kyber_adapter.address


@pytest.fixture
def kyber_feeds(http, dslo_protocol, client):
    """The maker API's sign-message (no fee, the API's predicate) and orders answers."""

    def kyber_sign_message(params, body):
        getters = linear_getters(int(body["makingAmount"]), int(body["takingAmount"]))
        message = {
            "salt": str(timestamp()),
            "makerAsset": body["makerAsset"],
            "takerAsset": body["takerAsset"],
            "maker": body["maker"],
            "receiver": body["receiver"],
            "allowedSender": "0x" + "00" * 20,
            "makingAmount": body["makingAmount"],
            "takingAmount": body["takingAmount"],
            "feeConfig": "0",
            "makerAssetData": "0x",
            "takerAssetData": "0x",
            "getMakerAmount": "0x" + getters[0].hex(),
            "getTakerAmount": "0x" + getters[1].hex(),
            "predicate": "0x"
            + api_predicate(dslo_protocol.address, body["maker"], 0, body["expiredAt"]).hex(),
            "interaction": "0x",
        }
        domain = {"chainId": client.chain_id, "verifyingContract": dslo_protocol.address}
        return {"code": 0, "message": "ok", "data": {"domain": domain, "message": message}}

    # The sign-message path contains the orders path: register it first.
    http.on("POST", SIGN_MESSAGE_PATH, kyber_sign_message)
    http.on("POST", ORDERS_PATH, CREATED)
    return http


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_publishes_records_and_reposts(
    rail_keeper,
    network_config,
    kyber_rail,
    kyber_feeds,
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
        "KyberDsloAdapter",
    }

    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", kyber_rail: "posted"}
    assert len(orderbook.posted) == 1 and len(orders_posted(kyber_feeds)) == 1
    record = state.order("boa", harness.address, kyber_rail, sell_token.address)
    assert record is not None
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.sell_amount == harness.available(sell_token.address)
    # timestampBelow is strict: the last fillable second is the bound minus one.
    assert record.valid_to == timestamp() + config.order_ttl - 1

    # Within the validity both rails hold; expired, a fresh message is signed
    # and posted at the lower curve price.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    posts = orders_posted(kyber_feeds)
    assert len(posts) == 2
    assert int(posts[1]["json"]["takingAmount"]) < int(posts[0]["json"]["takingAmount"])
    assert posts[1]["json"]["salt"] != posts[0]["json"]["salt"]


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, kyber_rail, kyber_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, kyber=KyberSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert kyber_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, kyber_rail, kyber_feeds, sell_token, lot, cow_adapter
):
    def boom(params, json):
        raise RuntimeError("sign-message exploded")

    kyber_feeds.handlers.insert(0, ("POST", SIGN_MESSAGE_PATH, boom))
    config = network_config(**whitelisted(sell_token))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        kyber_rail: "failed",
    }


def test_dry_run_posts_nothing(
    rail_keeper, network_config, kyber_rail, kyber_feeds, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    # Only the sign-message call reaches the API in a dry run.
    assert [c["url"].split("/")[-1] for c in kyber_feeds.calls] == ["sign-message"]
