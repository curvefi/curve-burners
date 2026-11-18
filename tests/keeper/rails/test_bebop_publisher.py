"""BebopJamPublisher against the real adapter, the JamSettlement mock and the
auction's signature router; the JAM quote and order calls are faked with a
``toSign`` in the shape the live API returned for a contract taker."""

import logging

import boa
import pytest
from eth_abi import encode

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    ERC1271_MAGIC_VALUE,
    JAM_ORDER_ABI,
    AdapterRegistry,
    Auction,
)
from keepers.auction_keeper.config import BebopSettings, TokenFilter
from keepers.auction_keeper.rails.bebop import BebopJamPublisher, JamOrder
from keepers.auction_keeper.rails.common import ApiError
from tests.burners.adapters.bebop.test_module import order_digest_reference
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

SOLVER = "0xbbf8e7cc7a29af9eb149beb9d83cfe015719f9ef"


@pytest.fixture(scope="module")
def jam_settlement():
    settlement = boa.load(
        "contracts/testing/dutch_auction/JamSettlementMock.vy",
        bytes(32),
        boa.env.generate_address("jam_protocol_fee"),
    )
    manager = boa.load("contracts/testing/dutch_auction/JamBalanceManagerMock.vy", settlement)
    settlement.set_balance_manager(manager.address)
    return settlement


@pytest.fixture
def bebop_adapter(jam_settlement):
    return boa.load("contracts/burners/adapters/bebop/BebopJamAdapter.vy", jam_settlement)


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def adapter(client, auction, bebop_adapter, jam_settlement, register):
    register(bebop_adapter, jam_settlement.balanceManager())
    registry = AdapterRegistry(client, auction.registry())
    return next(a for a in registry.active_adapters() if a.adapter == bebop_adapter.address)


@pytest.fixture
def quote_for(jam_settlement):
    """A JAM gasless quote for the lot, as the live API shaped it (2026-11-02)."""

    def _quote(view, *, buy_amount=None, expiry=None, **to_sign_overrides):
        buy_amount = view.amount_needed + 10**18 if buy_amount is None else buy_amount
        expiry = timestamp() + 60 if expiry is None else expiry
        to_sign = {
            "taker": view.auction,
            "receiver": view.receiver,
            "expiry": expiry,
            "exclusivityDeadline": expiry,
            "nonce": "195002664568054948611090147744406508035",
            "executor": SOLVER,
            "partnerInfo": "0",
            "sellTokens": [view.token],
            "buyTokens": [view.want],
            "sellAmounts": [str(view.available)],
            "buyAmounts": [str(buy_amount)],
            "hooksHash": "0x" + "00" * 32,
        }
        to_sign.update(to_sign_overrides)
        return {
            "requestId": "c37d83d8-e649-4fce-9c68-34759058e9ae",
            "type": "121",
            "status": "Success",
            "quoteId": "92b425a7-dddc-4739-870d-8c86fcd31203",
            "chainId": 1,
            "approvalType": "Standard",
            "nativeToken": "ETH",
            "taker": view.auction,
            "receiver": view.receiver,
            "expiry": expiry,
            "slippage": 0.2,
            "gasFee": {"native": "207372735825999", "usd": 0.46},
            "buyTokens": {
                view.want: {
                    "amount": str(buy_amount + 10**15),
                    "decimals": 18,
                    "symbol": "crvUSD",
                    "minimumAmount": str(buy_amount),
                }
            },
            "sellTokens": {
                view.token: {"amount": str(view.available), "decimals": 18, "symbol": "CRV"}
            },
            "settlementAddress": jam_settlement.address,
            "approvalTarget": jam_settlement.balanceManager(),
            "requiredSignatures": [],
            "warnings": [],
            "hooksHash": "0x" + "00" * 32,
            "toSign": to_sign,
            "solver": "\U0001f349",
        }

    return _quote


def test_quote_is_checked_signed_and_posted(
    client,
    auction,
    harness,
    network_config,
    http,
    adapter,
    bebop_adapter,
    jam_settlement,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    quote_for,
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", "/jam/ethereum/v2/quote", quote_for(view))
    http.on("POST", "/jam/ethereum/v2/order", {"status": "Pending", "txHash": None})
    config = network_config(bebop=BebopSettings(chain="ethereum"))
    publisher = BebopJamPublisher(client, adapter, config, http=http)
    assert publisher.settlement == jam_settlement.address
    assert publisher.balance_manager == jam_settlement.balanceManager()

    result = publisher.publish(auction, view)

    assert result is not None
    (quote_call,) = http.requests("GET", "/quote")
    assert quote_call["url"] == "https://api.bebop.xyz/jam/ethereum/v2/quote"
    assert quote_call["params"] == {
        "sell_tokens": sell_token.address,
        "buy_tokens": want.address,
        "sell_amounts": str(LOT_AMOUNT),
        "taker_address": harness.address,
        "receiver_address": proceeds_receiver,
        "approval_type": "Standard",
        "gasless": "true",
    }
    assert quote_call["headers"] == {}  # no key configured
    (order_call,) = http.requests("POST", "/order")
    assert order_call["url"] == "https://api.bebop.xyz/jam/ethereum/v2/order"
    body = order_call["json"]
    assert set(body) == {"quote_id", "signature", "sign_scheme"}
    assert body["quote_id"] == "92b425a7-dddc-4739-870d-8c86fcd31203" == result.uid
    assert body["sign_scheme"] == "EIP1271"
    # signature = adapter ++ abi_encode(toSign), valid on-chain for the JAM digest.
    order = JamOrder.from_to_sign(result.details["toSign"])
    signature = bytes.fromhex(body["signature"][2:])
    assert signature == bytes.fromhex(bebop_adapter.address[2:]) + encode(
        [JAM_ORDER_ABI], [order.as_tuple()]
    )
    digest = order_digest_reference(order.as_tuple(), publisher.domain_separator)
    assert order.digest(publisher.domain_separator) == digest
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.valid_to == timestamp() + 60
    assert result.sell_amount == LOT_AMOUNT and result.buy_amount == view.amount_needed + 10**18
    assert result.details["response"] == {"status": "Pending", "txHash": None}


def test_quote_below_the_curve_is_skipped(
    client, auction, network_config, http, adapter, sell_token, lot, quote_for, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    http.on("GET", "/quote", quote_for(view, buy_amount=view.amount_needed - 1))
    publisher = BebopJamPublisher(
        client, adapter, network_config(bebop=BebopSettings(chain="ethereum")), http=http
    )
    assert publisher.publish(auction, view) is None
    assert http.requests("POST", "/order") == []
    assert "below the curve quote" in caplog.text
    # Exactly the curve is fine: check_order's bound is inclusive.
    http.handlers.clear()
    http.on("GET", "/quote", quote_for(view, buy_amount=view.amount_needed))
    http.on("POST", "/order", {"status": "Pending"})
    assert publisher.publish(auction, view) is not None


def test_quotes_for_the_wrong_lot_or_receiver_are_skipped(
    client, auction, network_config, http, adapter, sell_token, other_token, lot, quote_for, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = BebopJamPublisher(
        client, adapter, network_config(bebop=BebopSettings(chain="ethereum")), http=http
    )
    for overrides, reason in (
        ({"receiver": "0x" + "11" * 20}, "is not the auction's receiver"),
        ({"sellTokens": [other_token.address]}, "are not the lot pair"),
        ({"sellAmounts": [str(LOT_AMOUNT + 1)]}, "exceeds the available"),
        ({"hooksHash": "0x" + "11" * 32}, "carries hooks"),
        ({"expiry": view.end + 1, "exclusivityDeadline": view.end + 1}, "past the lot end"),
    ):
        http.handlers.clear()
        http.on("GET", "/quote", quote_for(view, **overrides))
        assert publisher.publish(auction, view) is None, reason
        assert reason in caplog.text
    assert http.requests("POST", "/order") == []


def test_api_error_payloads_are_logged_not_raised(
    client, auction, network_config, http, adapter, sell_token, lot, quote_for, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    http.on(
        "GET",
        "/quote",
        {
            "error": {
                "errorCode": 108,
                "message": "RouteNotFound: no solutions found",
                "requestId": "x",
            }
        },
    )
    publisher = BebopJamPublisher(
        client, adapter, network_config(bebop=BebopSettings(chain="ethereum")), http=http
    )
    assert publisher.publish(auction, view) is None
    assert "RouteNotFound" in caplog.text

    http.handlers.clear()
    http.on("GET", "/quote", quote_for(view))
    http.on("POST", "/order", {"error": {"errorCode": 210, "message": "InvalidQuoteId"}})
    assert publisher.publish(auction, view) is None
    assert "InvalidQuoteId" in caplog.text

    http.handlers.clear()
    http.on("GET", "/quote", quote_for(view))

    def rejected(params, json):
        raise ApiError(400, "bad signature", "order")

    http.on("POST", "/order", rejected)
    assert publisher.publish(auction, view) is None
    assert "bad signature" in caplog.text


def test_api_key_is_sent_as_bearer_and_chain_defaults_by_id(
    client, auction, network_config, http, adapter, sell_token, lot, quote_for, monkeypatch
):
    view = auction.lot_view(sell_token.address)
    monkeypatch.setenv("BEBOP_API_KEY", "jam-key")
    http.on("GET", "/jam/ethereum/v2/quote", quote_for(view))
    http.on("POST", "/order", {"status": "Pending"})
    publisher = BebopJamPublisher(client, adapter, network_config(), http=http)
    assert client.chain_id == 1 and publisher.chain == "ethereum"
    assert publisher.publish(auction, view, dry_run=True) is not None
    assert http.requests("POST", "/order") == []
    assert http.calls[0]["headers"] == {"Authorization": "Bearer jam-key"}


def test_standing_ask_matches_order_for_when_not_posting(
    client, auction, harness, network_config, http, adapter, bebop_adapter, sell_token, lot
):
    publisher = BebopJamPublisher(
        client, adapter, network_config(order_ttl=10**6, bebop=BebopSettings(post=False)), http=http
    )
    view = auction.lot_view(sell_token.address)
    result = publisher.publish(auction, view)
    assert result is not None and http.calls == []
    expected, _ = bebop_adapter.order_for(harness.address, sell_token.address, 1)
    expected = JamOrder.from_tuple(tuple(expected))
    posted = result.details["order"]
    assert posted["taker"] == expected.taker == harness.address
    assert posted["receiver"] == expected.receiver
    assert posted["expiry"] == expected.expiry == view.end == result.valid_to
    assert posted["executor"] == "0x0000000000000000000000000000000000000000"
    assert posted["sellAmounts"] == [str(LOT_AMOUNT)] and posted["buyAmounts"] == [
        str(view.amount_needed)
    ]
    signature = bytes.fromhex(result.details["signature"][2:])
    assert harness.isValidSignature(bytes.fromhex(result.uid[2:]), signature) == ERC1271_MAGIC_VALUE

    short = BebopJamPublisher(
        client, adapter, network_config(order_ttl=120, bebop=BebopSettings(post=False)), http=http
    )
    result = short.publish(auction, view)
    assert result is not None and result.valid_to == timestamp() + 120


def test_keeper_mode_syncs_the_balance_manager_allowance(
    harness,
    network_config,
    http,
    bebop_adapter,
    jam_settlement,
    register,
    sell_token,
    lot,
    keeper_account,
    quote_for,
):
    register(bebop_adapter, jam_settlement.balanceManager())
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == bebop_adapter.address)
    view = auction.lot_view(sell_token.address)
    http.on("GET", "/quote", quote_for(view))
    http.on("POST", "/order", {"status": "Pending"})
    config = network_config(
        mode="keeper", keeper_key_env="KEY", bebop=BebopSettings(chain="ethereum")
    )
    publisher = BebopJamPublisher(client, adapter, config, http=http)
    manager = jam_settlement.balanceManager()
    assert sell_token.allowance(harness.address, manager) == 0
    assert publisher.publish(auction, view) is not None
    assert sell_token.allowance(harness.address, manager) == 2**256 - 1


# Config section


def test_config_section_defaults_and_parse(monkeypatch):
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    network = parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x")
    assert network.bebop.enabled and network.bebop.post and network.bebop.chain is None
    assert network.bebop.api_url == "https://api.bebop.xyz"
    monkeypatch.setenv("BEBOP_API_KEY", "k2")
    assert network.bebop.api_key == "k2"
    section = {"chain": "robinhood", "post": False, "api_key_env": None}
    network = parse_config(
        {"networks": {"x": {"rpc": "r", "auctions": [auction], "bebop": section}}}
    ).network("x")
    assert network.bebop.chain == "robinhood" and not network.bebop.post
    assert network.bebop.api_key is None
    for bad in (
        {"bebop": {"chain": 1}},
        {"bebop": {"nope": True}},
        {"bebop": {"post": "yes"}},
        {"bebop": "polygon"},
    ):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], **bad}}})


# Keeper cycle over the Bebop rail


@pytest.fixture
def bebop_rail(register, bebop_adapter, jam_settlement):
    register(bebop_adapter, jam_settlement.balanceManager())
    return bebop_adapter.address


@pytest.fixture
def bebop_feeds(http, harness, jam_settlement, sell_token, want, proceeds_receiver):
    """A JAM quote at the live curve price, expiring in 60 s, and an order POST."""

    def jam_quote(params, json):
        expiry = timestamp() + 60
        available = harness.available(sell_token.address)
        return {
            "quoteId": f"quote-{timestamp()}",
            "settlementAddress": jam_settlement.address,
            "solver": "s",
            "toSign": {
                "taker": harness.address,
                "receiver": proceeds_receiver,
                "expiry": expiry,
                "exclusivityDeadline": expiry,
                "nonce": str(timestamp()),
                "executor": SOLVER,
                "partnerInfo": "0",
                "sellTokens": [sell_token.address],
                "buyTokens": [want.address],
                "sellAmounts": [params["sell_amounts"]],
                "buyAmounts": [str(harness.getAmountNeeded(sell_token.address, available))],
                "hooksHash": "0x" + "00" * 32,
            },
        }

    http.on("GET", "/jam/", jam_quote)
    http.on("POST", "/jam/", {"status": "Pending"})
    return http


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_quotes_posts_and_renews_on_expiry(
    rail_keeper,
    network_config,
    bebop_rail,
    bebop_feeds,
    orderbook,
    sell_token,
    harness,
    lot,
    state,
    cow_adapter,
):
    config = network_config(
        **whitelisted(
            sell_token, poll_interval=60, order_ttl=300, bebop=BebopSettings(chain="ethereum")
        )
    )
    keeper = rail_keeper(config)
    assert {p.version for p in keeper.publishers_for(keeper.auctions[0])} == {
        "CowAdapter",
        "BebopJamAdapter",
    }

    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", bebop_rail: "posted"}
    assert len(orderbook.posted) == 1 and len(bebop_feeds.requests("POST", "/jam/")) == 1
    record = state.order("boa", harness.address, bebop_rail, sell_token.address)
    assert record is not None
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.sell_amount == harness.available(sell_token.address)
    assert record.valid_to == timestamp() + 60  # the quote's expiry, not the TTL

    # Nothing expires within the cycle: both rails hold, and the loop wakes
    # for the JAM quote's 60 s expiry rather than the poll interval.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    assert keeper.wait_seconds(0) == 60
    boa.env.time_travel(seconds=60)
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "kept", bebop_rail: "posted"}
    assert len(bebop_feeds.requests("POST", "/jam/")) == 2
    quotes = bebop_feeds.requests("GET", "/jam/")
    assert len(quotes) == 2 and quotes[1]["params"]["taker_address"] == harness.address


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, bebop_rail, bebop_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, bebop=BebopSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert bebop_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, bebop_rail, bebop_feeds, sell_token, lot, cow_adapter
):
    def boom(params, json):
        raise RuntimeError("quote exploded")

    bebop_feeds.handlers.insert(0, ("GET", "/jam/", boom))
    config = network_config(**whitelisted(sell_token, bebop=BebopSettings(chain="ethereum")))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        bebop_rail: "failed",
    }


def test_dry_run_posts_nothing(
    rail_keeper, network_config, bebop_rail, bebop_feeds, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token, bebop=BebopSettings(chain="ethereum")))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    # Only the quote reaches the feed in a dry run.
    assert [c["method"] for c in bebop_feeds.calls] == ["GET"]
