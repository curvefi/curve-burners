"""OKXIntentPublisher against the real adapter, the OKX settlement mock and the
auction's signature router; the quote and create-order calls are faked with
the shapes of the Onchain OS docs (Get Intent Quotes, Create Intent Order)."""

import base64
import hashlib
import hmac
import json
import logging

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    ERC1271_MAGIC_VALUE,
    OKX_ORDER_ABI,
    AdapterRegistry,
    Auction,
)
from keepers.auction_keeper.config import OKXSettings, TokenFilter
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.okx import (
    CREATE_ORDER_PATH,
    EXACT_IN,
    QUOTE_PATH,
    OKXIntentPublisher,
    OKXOrder,
)
from tests.burners.adapters.okx.test_module import order_digest_reference, order_uid_reference
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

ZERO = "0x0000000000000000000000000000000000000000"


@pytest.fixture(scope="module")
def okx_settlement():
    token_approve = boa.load("contracts/testing/dutch_auction/OKXTokenApproveMock.vy", ZERO)
    settlement = boa.load(
        "contracts/testing/dutch_auction/OKXSettlementMock.vy", bytes(32), token_approve.address
    )
    token_approve.set_allowed(settlement.address, True)
    return settlement


@pytest.fixture
def okx_adapter(okx_settlement):
    return boa.load("contracts/burners/adapters/okx/OKXIntentAdapter.vy", okx_settlement)


APP_DATA = "0xa1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
REFERRER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
# OKX's to-token commission flag (bit 254) with an off-chain label.
TO_TOKEN_FLAG = (1 << 254) | (1 << 248)
KEYS = {"OKX_API_KEY": "key-1", "OKX_SECRET_KEY": "secret-1", "OKX_PASSPHRASE": "pass-1"}


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def adapter(client, auction, okx_adapter, register):
    register(okx_adapter, okx_adapter.token_approve())
    registry = AdapterRegistry(client, auction.registry())
    return next(a for a in registry.active_adapters() if a.adapter == okx_adapter.address)


@pytest.fixture
def keys(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def quote_for(okx_settlement, client):
    """An intent quote in the shape of the docs' example (signData with the
    domain, the Order message and the types), around the live lot."""

    def _quote(view, *, to_amount=None, valid_to=None, settlement=None, **overrides):
        to_amount = view.amount_needed + 10**18 if to_amount is None else to_amount
        valid_to = timestamp() + 180 if valid_to is None else valid_to
        message = {
            "appData": APP_DATA,
            "commissionInfos": [
                {
                    "feePercent": "1000000",
                    "flag": str(TO_TOKEN_FLAG),
                    "referrerWalletAddress": REFERRER,
                }
            ],
            "fromTokenAddress": view.token.lower(),
            "fromTokenAmount": str(view.available),
            "owner": view.auction,
            "partiallyFillable": False,
            "receiver": view.receiver,
            "swapMode": "0x" + EXACT_IN.hex(),
            "toTokenAddress": view.want.lower(),
            "toTokenAmount": str(to_amount),
            "validTo": valid_to,
        }
        message.update(overrides)
        return {
            "code": "0",
            "data": [
                {
                    "chainIndex": str(client.chain_id),
                    "contextSlot": 0,
                    "dexRouterList": [],
                    "estimateGasFee": "0",
                    "fromTokenAmount": str(view.available),
                    "mode": "INTENT",
                    "priceImpactPercent": "-0.35",
                    "quoteId": "10000000000000001",
                    "router": f"{view.token}--{view.want}",
                    "signData": {
                        "domain": {
                            "chainId": client.chain_id,
                            "name": "OKX Intent Swap",
                            "verifyingContract": settlement or okx_settlement.address,
                            "version": "v1.0.0",
                        },
                        "message": message,
                        "primaryType": "Order",
                        "types": {},
                    },
                    "swapMode": "exactIn",
                    "toTokenAmount": str(to_amount + 10**16),
                    "tradeFee": "0",
                }
            ],
            "msg": "",
        }

    return _quote


CREATED = {"code": "0", "data": [{"orderUid": "0x" + "ab" * 56}], "msg": ""}


def test_quote_is_checked_signed_and_posted_with_hmac_headers(
    client,
    auction,
    harness,
    keys,
    http,
    adapter,
    okx_adapter,
    okx_settlement,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    quote_for,
    network_config,
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", CREATE_ORDER_PATH, CREATED)
    publisher = OKXIntentPublisher(client, adapter, network_config(), http=http)
    assert publisher.settlement == okx_settlement.address
    assert publisher.token_approve == okx_adapter.token_approve()
    assert publisher.chain_index == "1" and publisher.feed_ready

    result = publisher.publish(auction, view)

    assert result is not None
    (quote_call,) = http.requests("GET", QUOTE_PATH)
    assert quote_call["url"] == "https://web3.okx.com" + QUOTE_PATH
    assert quote_call["params"] == {
        "chainIndex": "1",
        "mode": "intent",
        "swapMode": "exactIn",
        "fromTokenAddress": sell_token.address,
        "toTokenAddress": want.address,
        "amount": str(LOT_AMOUNT),
        "userWalletAddress": harness.address,
        "swapReceiverAddress": proceeds_receiver,
        "signingScheme": "eip1271",
        "slippagePercent": "0.1",
        "expiration": "180",
    }
    (order_call,) = http.requests("POST", CREATE_ORDER_PATH)
    body = order_call["json"]
    assert set(body) == {
        "chainIndex",
        "fromTokenAddress",
        "toTokenAddress",
        "fromTokenAmount",
        "toTokenAmount",
        "userWalletAddress",
        "swapReceiverAddress",
        "validTo",
        "quoteId",
        "appData",
        "signingScheme",
        "signature",
        "commissionInfos",
    }
    assert body["quoteId"] == "10000000000000001" and body["signingScheme"] == "eip1271"
    assert (
        body["userWalletAddress"] == harness.address
        and body["swapReceiverAddress"] == proceeds_receiver
    )
    assert body["fromTokenAmount"] == str(LOT_AMOUNT)
    assert body["toTokenAmount"] == str(view.amount_needed + 10**18) == str(result.buy_amount)
    assert body["validTo"] == timestamp() + 180 == result.valid_to
    assert body["appData"] == APP_DATA
    assert body["commissionInfos"] == [
        {"feePercent": "1000000", "referrerWalletAddress": REFERRER, "flag": str(TO_TOKEN_FLAG)}
    ]
    # Posted signature = burner ++ adapter ++ abi_encode(order) (owner_prefix default).
    order = OKXOrder.from_message(quote_for(view)["data"][0]["signData"]["message"])
    bare = bytes.fromhex(okx_adapter.address[2:]) + encode([OKX_ORDER_ABI], [order.as_tuple()])
    assert bytes.fromhex(body["signature"][2:]) == bytes.fromhex(harness.address[2:]) + bare
    assert result.details["signature"] == "0x" + bare.hex()
    # The bare pair validates on-chain for the settlement's digest; uid as the settlement keys it.
    digest = order_digest_reference(order.as_tuple(), publisher.domain_separator)
    assert order.digest(publisher.domain_separator) == digest
    assert harness.isValidSignature(digest, bare) == ERC1271_MAGIC_VALUE
    described_order, described_digest, described_uid = okx_adapter.describe(bare[20:])
    assert bytes(described_digest) == digest
    assert bytes(described_uid) == order_uid_reference(digest, harness.address, order.valid_to)
    assert result.details["orderUid"] == "0x" + bytes(described_uid).hex()
    assert result.uid == CREATED["data"][0]["orderUid"]  # the API's id wins once created
    # HMAC headers on both calls: base64(HMAC-SHA256(ts + METHOD + path[?query] + body)).
    for call, method, path in (
        (quote_call, "GET", QUOTE_PATH),
        (order_call, "POST", CREATE_ORDER_PATH),
    ):
        headers = call["headers"]
        assert headers["OK-ACCESS-KEY"] == "key-1" and headers["OK-ACCESS-PASSPHRASE"] == "pass-1"
        assert (
            headers["OK-ACCESS-TIMESTAMP"].endswith("Z") and "T" in headers["OK-ACCESS-TIMESTAMP"]
        )
        query = (
            "?" + "&".join(f"{k}={v}" for k, v in call["params"].items()) if call["params"] else ""
        )
        prehash = headers["OK-ACCESS-TIMESTAMP"] + method + path + query + (call["data"] or "")
        expected = base64.b64encode(
            hmac.new(b"secret-1", prehash.encode(), hashlib.sha256).digest()
        ).decode()
        assert headers["OK-ACCESS-SIGN"] == expected
    assert order_call["data"] == json.dumps(body, separators=(",", ":"))


def test_owner_prefix_switch(
    client,
    auction,
    harness,
    keys,
    http,
    adapter,
    okx_adapter,
    sell_token,
    lot,
    quote_for,
    network_config,
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", CREATE_ORDER_PATH, CREATED)
    publisher = OKXIntentPublisher(
        client, adapter, network_config(okx=OKXSettings(owner_prefix=False)), http=http
    )
    result = publisher.publish(auction, view)
    assert result is not None and result.details["ownerPrefix"] is False
    (order_call,) = http.requests("POST", CREATE_ORDER_PATH)
    assert order_call["json"]["signature"] == result.details["signature"]
    assert order_call["json"]["signature"].startswith("0x" + okx_adapter.address[2:].lower())


def test_quote_below_the_curve_or_for_another_lot_is_skipped(
    client,
    auction,
    keys,
    http,
    adapter,
    sell_token,
    other_token,
    lot,
    quote_for,
    network_config,
    caplog,
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = OKXIntentPublisher(client, adapter, network_config(), http=http)
    cases = [
        (dict(to_amount=view.amount_needed - 1), "below the curve quote"),
        (dict(settlement="0x" + "12" * 20), "is not the adapter's settlement"),
        (dict(receiver="0x" + "11" * 20), "is not the auction's receiver"),
        (dict(fromTokenAddress=other_token.address), "are not the lot pair"),
        (dict(fromTokenAmount=str(LOT_AMOUNT + 1)), "exceeds the available"),
        (dict(partiallyFillable=True), "not an exactIn fill-or-kill"),
        (dict(valid_to=view.end + 1), "past the lot end"),
    ]
    for kwargs, reason in cases:
        http.handlers.clear()
        http.on("GET", QUOTE_PATH, quote_for(view, **kwargs))
        assert publisher.publish(auction, view) is None, reason
        assert reason in caplog.text
    assert http.requests("POST", CREATE_ORDER_PATH) == []
    # Exactly the curve is fine.
    http.handlers.clear()
    http.on("GET", QUOTE_PATH, quote_for(view, to_amount=view.amount_needed))
    http.on("POST", CREATE_ORDER_PATH, CREATED)
    assert publisher.publish(auction, view) is not None


def test_api_envelope_errors_are_logged_not_raised(
    client, auction, keys, http, adapter, sell_token, lot, quote_for, network_config, caplog
):
    view = auction.lot_view(sell_token.address)
    publisher = OKXIntentPublisher(client, adapter, network_config(), http=http)
    http.on("GET", QUOTE_PATH, {"code": "50011", "data": [], "msg": "Invalid Sign"})
    assert publisher.publish(auction, view) is None
    assert "Invalid Sign" in caplog.text

    http.handlers.clear()
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on(
        "POST", CREATE_ORDER_PATH, {"code": "82000", "data": [], "msg": "invalid eip1271 signature"}
    )
    assert publisher.publish(auction, view) is None
    assert "invalid eip1271 signature" in caplog.text

    http.handlers.clear()
    http.on("GET", QUOTE_PATH, quote_for(view))

    def rejected(params, json):
        raise ApiError(401, "unauthorized", CREATE_ORDER_PATH)

    http.on("POST", CREATE_ORDER_PATH, rejected)
    assert publisher.publish(auction, view) is None
    assert "unauthorized" in caplog.text


def test_without_keys_or_chain_index_only_standing_asks(
    client,
    auction,
    harness,
    http,
    adapter,
    okx_adapter,
    sell_token,
    lot,
    network_config,
    monkeypatch,
    caplog,
):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    publisher = OKXIntentPublisher(client, adapter, network_config(order_ttl=10**6), http=http)
    assert not publisher.feed_ready and "passphrase missing" in caplog.text
    view = auction.lot_view(sell_token.address)
    result = publisher.publish(auction, view)
    assert result is not None and http.calls == []
    expected, _ = okx_adapter.order_for(harness.address, sell_token.address, 0, bytes(32))
    expected = OKXOrder.from_tuple(tuple(expected))
    order = result.details["order"]
    assert order["owner"] == expected.owner == harness.address
    assert order["receiver"] == expected.receiver
    assert order["validTo"] == expected.valid_to == view.end == result.valid_to
    assert order["fromTokenAmount"] == str(LOT_AMOUNT) and order["toTokenAmount"] == str(
        view.amount_needed
    )
    assert order["commissionInfos"] == [] and order["swapMode"] == "0x" + EXACT_IN.hex()
    assert order["appData"] != "0x" + "00" * 32  # a salt
    signature = bytes.fromhex(result.details["signature"][2:])
    digest = bytes.fromhex(result.uid[2:])[:32]
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE

    # Keys but an unlisted chain: the same.
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    publisher = OKXIntentPublisher(
        client, adapter, network_config(order_ttl=120, okx=OKXSettings(chain_index=None)), http=http
    )
    monkeypatch.setattr(publisher, "chain_index", None)
    result = publisher.publish(auction, view)
    assert result is not None and http.calls == [] and result.valid_to == timestamp() + 120


def test_post_false_is_standing_asks_and_dry_run_posts_nothing(
    client, auction, keys, http, adapter, sell_token, lot, quote_for, network_config
):
    view = auction.lot_view(sell_token.address)
    publisher = OKXIntentPublisher(
        client, adapter, network_config(okx=OKXSettings(post=False)), http=http
    )
    assert publisher.publish(auction, view) is not None and http.calls == []
    http.on("GET", QUOTE_PATH, quote_for(view))
    publisher = OKXIntentPublisher(client, adapter, network_config(), http=http)
    assert publisher.publish(auction, view, dry_run=True) is not None
    assert http.requests("POST", CREATE_ORDER_PATH) == []


def test_keeper_mode_syncs_the_token_approve_allowance(
    harness,
    keys,
    http,
    okx_adapter,
    register,
    sell_token,
    lot,
    keeper_account,
    quote_for,
    network_config,
):
    register(okx_adapter, okx_adapter.token_approve())
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == okx_adapter.address)
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", CREATE_ORDER_PATH, CREATED)
    publisher = OKXIntentPublisher(
        client, adapter, network_config(mode="keeper", keeper_key_env="KEY"), http=http
    )
    token_approve = okx_adapter.token_approve()
    assert sell_token.allowance(harness.address, token_approve) == 0
    assert publisher.publish(auction, view) is not None
    assert sell_token.allowance(harness.address, token_approve) == 2**256 - 1


def test_order_hashing_matches_the_module_reference(harness, okx_adapter, sell_token, lot):
    built, signature = okx_adapter.order_for(
        harness.address, sell_token.address, 0, keccak(b"salt")
    )
    order = OKXOrder.from_tuple(tuple(built))
    domain = bytes(okx_adapter.domain_separator())
    assert order.signature(okx_adapter.address) == bytes(signature)
    assert order.digest(domain) == order_digest_reference(order.as_tuple(), domain)
    assert order.uid(domain) == order_uid_reference(
        order.digest(domain), harness.address, order.valid_to
    )


# Config section


def test_config_section_defaults_and_parse(monkeypatch):
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    network = parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x")
    assert network.okx.enabled and network.okx.post
    assert network.okx.owner_prefix and network.okx.expiration == 180
    assert network.okx.chain_index is None and network.okx.slippage_percent == "0.1"
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    assert (network.okx.api_key, network.okx.secret_key, network.okx.passphrase) == (
        None,
        None,
        None,
    )

    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    section = {"owner_prefix": False, "chain_index": "196", "expiration": 300}
    okx = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], "okx": section}}})
        .network("x")
        .okx
    )
    assert not okx.owner_prefix and okx.chain_index == "196" and okx.expiration == 300
    assert (okx.api_key, okx.secret_key, okx.passphrase) == ("key-1", "secret-1", "pass-1")
    for bad in (
        {"okx": {"expiration": 0}},
        {"okx": {"chain_index": 1}},
        {"okx": {"owner_prefix": "yes"}},
        {"okx": {"nope": True}},
        {"okx": "polygon"},
    ):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], **bad}}})


# Keeper cycle over the OKX rail


@pytest.fixture
def okx_rail(register, okx_adapter, keys):
    register(okx_adapter, okx_adapter.token_approve())
    return okx_adapter.address


@pytest.fixture
def okx_feeds(http, harness, okx_settlement, client, sell_token, want, proceeds_receiver):
    """An intent quote at the live curve price (valid 180 s) and a create-order answer."""

    def okx_quote(params, json):
        available = harness.available(sell_token.address)
        message = {
            "appData": "0x" + "a1" * 32,
            "commissionInfos": [],
            "fromTokenAddress": sell_token.address,
            "fromTokenAmount": params["amount"],
            "owner": harness.address,
            "partiallyFillable": False,
            "receiver": proceeds_receiver,
            "swapMode": "0x" + EXACT_IN.hex(),
            "toTokenAddress": want.address,
            "toTokenAmount": str(harness.getAmountNeeded(sell_token.address, available)),
            "validTo": timestamp() + 180,
        }
        domain = {
            "chainId": client.chain_id,
            "name": "OKX Intent Swap",
            "verifyingContract": okx_settlement.address,
            "version": "v1.0.0",
        }
        sign_data = {"domain": domain, "message": message, "primaryType": "Order", "types": {}}
        return {"code": "0", "data": [{"quoteId": "q1", "signData": sign_data}], "msg": ""}

    http.on("GET", QUOTE_PATH, okx_quote)
    http.on("POST", CREATE_ORDER_PATH, CREATED)
    return http


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_publishes_records_and_reposts(
    rail_keeper,
    network_config,
    okx_rail,
    okx_feeds,
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
        "OKXIntentAdapter",
    }

    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", okx_rail: "posted"}
    assert len(orderbook.posted) == 1
    assert len(okx_feeds.requests("POST", CREATE_ORDER_PATH)) == 1
    record = state.order("boa", harness.address, okx_rail, sell_token.address)
    assert record is not None
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.sell_amount == harness.available(sell_token.address)
    assert record.valid_to == timestamp() + 180  # the quote's validTo

    # Within the validity both rails hold; expired, the OKX order is quoted
    # and posted again at the fresh (lower) curve price.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    posts = okx_feeds.requests("POST", CREATE_ORDER_PATH)
    assert len(posts) == 2
    assert int(posts[1]["json"]["toTokenAmount"]) < int(posts[0]["json"]["toTokenAmount"])
    assert all("OK-ACCESS-SIGN" in c["headers"] for c in okx_feeds.calls)


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, okx_rail, okx_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, okx=OKXSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert okx_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, okx_rail, okx_feeds, sell_token, lot, cow_adapter
):
    def boom(params, json):
        raise RuntimeError("quote exploded")

    okx_feeds.handlers.insert(0, ("GET", QUOTE_PATH, boom))
    config = network_config(**whitelisted(sell_token))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        okx_rail: "failed",
    }


def test_dry_run_posts_nothing(
    rail_keeper, network_config, okx_rail, okx_feeds, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    assert [c["method"] for c in okx_feeds.calls] == ["GET"]
