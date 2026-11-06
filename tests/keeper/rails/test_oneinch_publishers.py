"""The three 1inch publishers against the real adapters, the LOP mock and
the auction's signature router; the orderbook and Fusion HTTP calls are faked."""

import logging

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import ERC1271_MAGIC_VALUE, AdapterRegistry, Auction
from keepers.auction_keeper.config import OneInchSettings, TokenFilter
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.oneinch import (
    FUSION_EXPIRATION_DELAY,
    OneInchFeeTakerPublisher,
    OneInchFusionPublisher,
    OneInchOrder,
    OneInchPublisher,
    encode_auction_details,
    encode_fees,
    encode_surplus,
    fee_info_extension,
    fusion_extension,
    fusion_quote_extension,
    track_code,
    whitelist_delays,
)
from tests.burners.adapters.oneinch.test_module import order_digest_reference
from tests.burners.adapters.oneinch_feetaker import test_module as feetaker_vectors
from tests.burners.adapters.oneinch_fusion import test_module as fusion_vectors
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

ZERO = "0x0000000000000000000000000000000000000000"
ROUTER_NAME = "1inch Aggregation Router"
ROUTER_VERSION = "6"


@pytest.fixture(scope="module")
def oneinch_protocol():
    return boa.load(
        "contracts/testing/dutch_auction/LimitOrderProtocolMock.vy",
        ROUTER_NAME.encode(),
        ROUTER_VERSION.encode(),
        b"\x0f",
        bytes(32),
    )


@pytest.fixture(scope="module")
def fee_taker_mock(oneinch_protocol):
    return boa.load(
        "contracts/testing/dutch_auction/FeeTakerMock.vy", oneinch_protocol, ZERO, False
    )


@pytest.fixture(scope="module")
def fusion_settlement_mock(oneinch_protocol):
    return boa.load("contracts/testing/dutch_auction/FeeTakerMock.vy", oneinch_protocol, ZERO, True)


@pytest.fixture
def oneinch_adapter(oneinch_protocol):
    return boa.load("contracts/burners/adapters/oneinch/OneInchAdapter.vy", oneinch_protocol)


@pytest.fixture
def fee_taker_adapter(oneinch_protocol, fee_taker_mock):
    return boa.load(
        "contracts/burners/adapters/oneinch/OneInchFeeTakerAdapter.vy",
        oneinch_protocol,
        fee_taker_mock,
    )


@pytest.fixture
def fusion_adapter(oneinch_protocol, fusion_settlement_mock):
    return boa.load(
        "contracts/burners/adapters/oneinch/OneInchFusionAdapter.vy",
        oneinch_protocol,
        fusion_settlement_mock,
    )


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


def adapter_config(client, auction, adapter):
    registry = AdapterRegistry(client, auction.registry())
    return next(a for a in registry.active_adapters() if a.adapter == adapter.address)


@pytest.fixture
def fee_info(fee_taker_mock, proceeds_receiver):
    """A ``fee-info`` answer in the SDK's FeeInfoDTO shape: resolver fee 1%
    with a 50% whitelist discount, an integrator fee of 0.5% shared 40%."""
    return {
        "whitelist": {
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
        "feeBps": 100,
        "whitelistDiscountPercent": 50,
        "protocolFeeReceiver": "0x7777777777777777777777777777777777777777",
        "extensionAddress": fee_taker_mock.address,
        "integratorFeeBps": 50,
        "integratorFeeReceiver": "0x8888888888888888888888888888888888888888",
        "integratorFeeSharePercent": 40,
        "source": "aba10994",
    }


def fusion_quote_for(settlement, quote_amount, params, body, *, start_in=0, resolver=None):
    """A ``quote/receive`` answer in the SDK's QuoterResponse shape around the
    custom preset the publisher asked for."""
    preset = body["customPreset"]
    return {
        "fromTokenAmount": params["amount"],
        "toTokenAmount": preset["auctionStartAmount"],
        "marketAmount": str(quote_amount + 10**18),
        "presets": {
            "fast": {},
            "medium": {},
            "slow": {},
            "custom": {
                "auctionDuration": preset["auctionDuration"],
                "startAuctionIn": start_in,
                "bankFee": "0",
                "initialRateBump": 100_000,
                "auctionStartAmount": preset["auctionStartAmount"],
                "auctionEndAmount": preset["auctionEndAmount"],
                "tokenFee": "0",
                "points": [
                    {"delay": 60, "coefficient": 60_000},
                    {"delay": 60, "coefficient": 20_000},
                ],
                "allowPartialFills": True,
                "allowMultipleFills": True,
                "gasCost": {"gasBumpEstimate": 1234, "gasPriceEstimate": "1000"},
                "exclusiveResolver": None,
            },
        },
        "recommended_preset": "custom",
        "prices": {"usd": {"fromToken": "1", "toToken": "1"}},
        "volume": {"usd": {"fromToken": "1", "toToken": "1"}},
        "settlementAddress": settlement,
        "whitelist": [resolver or "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        "quoteId": "11111111-2222-3333-4444-555555555555",
        "autoK": 1,
        "fee": {
            "receiver": "0x7777777777777777777777777777777777777777",
            "bps": 100,
            "whitelistDiscountPercent": 50,
        },
        "surplusFee": 50,
        "integratorFee": 0,
        "integratorFeeShare": 0,
        "source": "0xaba10994",
    }


# Extension encoders against the SDK vectors


def test_fee_info_extension_matches_the_sdk_bytes():
    info = {
        "whitelist": {"w1": feetaker_vectors.SDK_RESOLVER_A, "w2": feetaker_vectors.SDK_RESOLVER_B},
        "feeBps": 100,
        "whitelistDiscountPercent": 50,
        "protocolFeeReceiver": feetaker_vectors.SDK_PROTOCOL,
        "extensionAddress": feetaker_vectors.FEE_TAKER,
        "integratorFeeBps": 50,
        "integratorFeeReceiver": feetaker_vectors.SDK_INTEGRATOR,
        "integratorFeeSharePercent": 40,
        "source": "aba10994",
    }
    assert fee_info_extension(info, feetaker_vectors.SDK_RECEIVER) == feetaker_vectors.SDK_EXTENSION
    # A malformed integrator block (fee without share) falls back to no integrator fee.
    partial = {**info, "integratorFeeSharePercent": None}
    assert (
        fee_info_extension(partial, feetaker_vectors.SDK_RECEIVER) != feetaker_vectors.SDK_EXTENSION
    )
    assert encode_fees(0, 0, 1000, 50) in fee_info_extension(partial, feetaker_vectors.SDK_RECEIVER)


def test_fusion_extension_matches_the_sdk_bytes():
    start = fusion_vectors.SDK_START
    whitelist = whitelist_delays(
        start,
        [(fusion_vectors.SDK_RESOLVER_A, start), (fusion_vectors.SDK_RESOLVER_B, start + 30)],
    )
    assert whitelist == [(fusion_vectors.SDK_RESOLVER_A, 0), (fusion_vectors.SDK_RESOLVER_B, 30)]
    extension = fusion_extension(
        fusion_vectors.SETTLEMENT,
        encode_auction_details(start, 180, 100_000, [(60_000, 60), (20_000, 60)], 1234, 1000),
        encode_fees(500, 40, 1000, 50),
        whitelist,
        start,
        fusion_vectors.SDK_INTEGRATOR,
        fusion_vectors.SDK_PROTOCOL,
        fusion_vectors.SDK_RECEIVER,
        encode_surplus(fusion_vectors.SDK_ESTIMATE, 50),
    )
    assert extension == fusion_vectors.SDK_EXTENSION


def test_track_code_follows_the_sdk():
    assert track_code(None) == 0
    assert track_code("0xaba10994") == 0xABA10994
    assert track_code("0x" + "ab" * 32) == 0xABABABAB
    assert track_code("aba10994") == int.from_bytes(keccak(text="aba10994")[:4], "big")


# Plain orders


def test_plain_order_matches_order_for_and_validates(
    client,
    auction,
    harness,
    network_config,
    http,
    oneinch_adapter,
    oneinch_protocol,
    register,
    sell_token,
    lot,
):
    register(oneinch_adapter, oneinch_protocol)
    config = network_config(order_ttl=10**6, oneinch=OneInchSettings(post=False))
    publisher = OneInchPublisher(
        client, adapter_config(client, auction, oneinch_adapter), config, http=http
    )
    assert publisher.protocol == oneinch_protocol.address
    view = auction.lot_view(sell_token.address)

    result = publisher.publish(auction, view)

    assert result is not None and http.calls == []
    expected, expected_signature = oneinch_adapter.order_for(harness.address, sell_token.address, 1)
    order = OneInchOrder.from_tuple(tuple(expected))
    posted = result.details["order"]
    assert [posted[k] for k in ("maker", "receiver", "makerAsset", "takerAsset")] == [
        order.maker,
        order.receiver,
        order.maker_asset,
        order.taker_asset,
    ]
    assert int(posted["makingAmount"]) == LOT_AMOUNT == result.sell_amount
    assert int(posted["takingAmount"]) == order.taking_amount == result.buy_amount
    assert int(posted["makerTraits"]) == order.maker_traits  # the lot end: TTL is longer
    assert result.valid_to == view.end
    # The signature validates on-chain for the protocol's digest.
    signature = bytes.fromhex(result.details["signature"][2:])
    assert signature[:20] == bytes.fromhex(oneinch_adapter.address[2:])
    rebuilt = OneInchOrder(**{**order.__dict__, "salt": int(posted["salt"])})
    digest = order_digest_reference(rebuilt.as_tuple(), publisher.domain_separator)
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.uid == "0x" + digest.hex()


def test_plain_ttl_shortens_expiration_and_posts_when_asked(
    client,
    auction,
    network_config,
    http,
    oneinch_adapter,
    oneinch_protocol,
    register,
    sell_token,
    lot,
    monkeypatch,
):
    register(oneinch_adapter, oneinch_protocol)
    monkeypatch.setenv("ONEINCH_API_KEY", "dev-portal-key")
    http.on("POST", "/orderbook/v4.0/", {"success": True})
    config = network_config(order_ttl=300, oneinch=OneInchSettings(post=True))
    publisher = OneInchPublisher(
        client, adapter_config(client, auction, oneinch_adapter), config, http=http
    )
    view = auction.lot_view(sell_token.address)

    result = publisher.publish(auction, view)

    assert result is not None and result.valid_to == timestamp() + 300
    (call,) = http.calls
    assert call["url"].endswith(f"/orderbook/v4.0/{client.chain_id}")
    assert call["headers"] == {"Authorization": "Bearer dev-portal-key"}
    body = call["json"]
    assert body["orderHash"] == result.uid
    assert body["signature"] == result.details["signature"]
    assert set(body["data"]) == {
        "salt",
        "maker",
        "receiver",
        "makerAsset",
        "takerAsset",
        "makingAmount",
        "takingAmount",
        "makerTraits",
        "extension",
    }
    assert body["data"]["extension"] == "0x"
    expiration = (int(body["data"]["makerTraits"]) >> 80) & (2**40 - 1)
    assert expiration == timestamp() + 300


def test_plain_underpriced_order_is_reported(
    client,
    auction,
    harness,
    network_config,
    http,
    oneinch_adapter,
    oneinch_protocol,
    register,
    sell_token,
    lot,
):
    register(oneinch_adapter, oneinch_protocol)
    publisher = OneInchPublisher(
        client, adapter_config(client, auction, oneinch_adapter), network_config(), http=http
    )
    built, _ = oneinch_adapter.order_for(harness.address, sell_token.address, 7)
    order = OneInchOrder.from_tuple(tuple(built))
    cheap = OneInchOrder(**{**order.__dict__, "taking_amount": order.taking_amount - 1})
    reason = publisher.validate_signature(
        auction, cheap.digest(publisher.domain_separator), cheap.signature(oneinch_adapter.address)
    )
    assert reason == "BadBuyAmount()"


def test_plain_keeper_mode_syncs_the_router_allowance(
    harness,
    network_config,
    http,
    oneinch_adapter,
    oneinch_protocol,
    register,
    sell_token,
    lot,
    keeper_account,
):
    register(oneinch_adapter, oneinch_protocol)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    config = network_config(
        mode="keeper", keeper_key_env="KEY", oneinch=OneInchSettings(post=False)
    )
    publisher = OneInchPublisher(
        client, adapter_config(client, auction, oneinch_adapter), config, http=http
    )
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is not None
    assert sell_token.allowance(harness.address, oneinch_protocol.address) == 2**256 - 1


# FeeTaker orderbook orders


def test_fee_taker_publishes_the_fee_info_extension(
    client,
    auction,
    harness,
    network_config,
    http,
    fee_taker_adapter,
    fee_taker_mock,
    oneinch_protocol,
    register,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    fee_info,
):
    register(fee_taker_adapter, oneinch_protocol)
    http.on("GET", "/fee-info", fee_info)
    http.on("POST", "/orderbook/v4.0/", {"success": True})
    config = network_config(order_ttl=300)
    publisher = OneInchFeeTakerPublisher(
        client, adapter_config(client, auction, fee_taker_adapter), config, http=http
    )
    assert publisher.fee_taker == fee_taker_mock.address
    view = auction.lot_view(sell_token.address)

    result = publisher.publish(auction, view)

    assert result is not None
    (info_call,) = http.requests("GET", "/fee-info")
    assert info_call["params"] == {
        "makerAsset": sell_token.address,
        "takerAsset": want.address,
        "makerAmount": str(LOT_AMOUNT),
        "takerAmount": str(view.amount_needed),
    }
    (post,) = http.requests("POST", "/orderbook/v4.0/")
    data = post["json"]["data"]
    extension = bytes.fromhex(data["extension"][2:])
    assert extension == fee_info_extension(fee_info, proceeds_receiver)
    assert data["receiver"] == fee_taker_mock.address
    assert (
        int(data["makingAmount"]) == LOT_AMOUNT and int(data["takingAmount"]) == view.amount_needed
    )
    salt = int(data["salt"])
    assert salt >> 224 == track_code("aba10994")  # SDK track code bits
    assert salt & (2**160 - 1) == int.from_bytes(keccak(extension), "big") & (2**160 - 1)
    traits = int(data["makerTraits"])
    assert (traits >> 80) & (2**40 - 1) == timestamp() + 300
    assert traits & (1 << 249) and traits & (1 << 251)  # extension + post-interaction flags
    # The adapter describes the extension the way the orderbook configured it.
    fees, receivers, size = fee_taker_adapter.describe(extension)
    assert tuple(fees) == (500, 40, 1000, 50) and size == 2
    assert tuple(receivers)[2] == proceeds_receiver
    # Signature = adapter ++ abi_encode(order, extension), valid for the router's digest.
    order_tuple = tuple(
        int(data[k]) if k in ("salt", "makingAmount", "takingAmount", "makerTraits") else data[k]
        for k in (
            "salt",
            "maker",
            "receiver",
            "makerAsset",
            "takerAsset",
            "makingAmount",
            "takingAmount",
            "makerTraits",
        )
    )
    signature = bytes.fromhex(post["json"]["signature"][2:])
    assert signature == bytes.fromhex(fee_taker_adapter.address[2:]) + encode(
        ["(uint256,address,address,address,address,uint256,uint256,uint256)", "bytes"],
        [order_tuple, extension],
    )
    digest = order_digest_reference(order_tuple, publisher.domain_separator)
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert post["json"]["orderHash"] == "0x" + digest.hex() == result.uid


def test_fee_taker_refuses_a_foreign_extension_contract(
    client,
    auction,
    network_config,
    http,
    fee_taker_adapter,
    oneinch_protocol,
    register,
    sell_token,
    lot,
    fee_info,
    caplog,
):
    caplog.set_level(logging.INFO)
    register(fee_taker_adapter, oneinch_protocol)
    http.on("GET", "/fee-info", {**fee_info, "extensionAddress": "0x" + "12" * 20})
    publisher = OneInchFeeTakerPublisher(
        client, adapter_config(client, auction, fee_taker_adapter), network_config(), http=http
    )
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is None
    assert http.requests("POST", "/orderbook") == []
    assert "is not the adapter's" in caplog.text


def test_fee_taker_api_errors_are_logged_not_raised(
    client,
    auction,
    network_config,
    http,
    fee_taker_adapter,
    oneinch_protocol,
    register,
    sell_token,
    lot,
    fee_info,
    caplog,
):
    caplog.set_level(logging.INFO)
    register(fee_taker_adapter, oneinch_protocol)

    def rejected(params, json):
        raise ApiError(401, {"message": "Unauthorized"}, "fee-info")

    http.on("GET", "/fee-info", rejected)
    publisher = OneInchFeeTakerPublisher(
        client, adapter_config(client, auction, fee_taker_adapter), network_config(), http=http
    )
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is None
    assert "fee-info failed" in caplog.text

    http.handlers.clear()
    http.on("GET", "/fee-info", fee_info)

    def bad_order(params, json):
        raise ApiError(400, {"description": "order invalid"}, "orderbook")

    http.on("POST", "/orderbook/v4.0/", bad_order)
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is None
    assert "orderbook" in caplog.text and "rejected" in caplog.text


# Fusion orders


def test_fusion_quotes_with_a_custom_preset_and_submits(
    client,
    auction,
    harness,
    network_config,
    http,
    fusion_adapter,
    fusion_settlement_mock,
    oneinch_protocol,
    register,
    sell_token,
    want,
    proceeds_receiver,
    lot,
):
    register(fusion_adapter, oneinch_protocol)
    view = auction.lot_view(sell_token.address)
    http.on(
        "POST",
        "/quote/receive",
        lambda params, body: fusion_quote_for(
            fusion_settlement_mock.address, view.amount_needed, params, body, start_in=5
        ),
    )
    http.on("POST", "/order/submit", {"ok": True})
    config = network_config(
        order_ttl=600,
        oneinch=OneInchSettings(fusion_auction_duration=180, fusion_start_premium_bps=100),
    )
    publisher = OneInchFusionPublisher(
        client, adapter_config(client, auction, fusion_adapter), config, http=http
    )
    assert publisher.settlement == fusion_settlement_mock.address

    result = publisher.publish(auction, view)

    assert result is not None
    (quote_call,) = http.requests("POST", "/quote/receive")
    assert quote_call["url"].endswith(f"/fusion/quoter/v2.0/{client.chain_id}/quote/receive")
    assert quote_call["params"] == {
        "fromTokenAddress": sell_token.address,
        "toTokenAddress": want.address,
        "amount": str(LOT_AMOUNT),
        "walletAddress": harness.address,
        "surplus": "true",
    }
    preset = quote_call["json"]["customPreset"]
    assert preset == {
        "auctionDuration": 180,
        "auctionStartAmount": str(view.amount_needed * 10100 // 10000),
        "auctionEndAmount": str(view.amount_needed),
    }
    (submit,) = http.requests("POST", "/order/submit")
    assert submit["url"].endswith(f"/fusion/relayer/v2.0/{client.chain_id}/order/submit")
    body = submit["json"]
    assert set(body) == {"order", "signature", "quoteId", "extension"}
    assert body["quoteId"] == "11111111-2222-3333-4444-555555555555"
    assert body["order"]["receiver"] == fusion_settlement_mock.address
    assert int(body["order"]["takingAmount"]) == view.amount_needed
    extension = bytes.fromhex(body["extension"][2:])
    fees, receivers, size, details, surplus = fusion_adapter.describe(extension)
    assert tuple(fees) == (0, 0, 1000, 50) and size == 1
    assert tuple(receivers) == (
        ZERO,
        "0x7777777777777777777777777777777777777777",
        proceeds_receiver,
    )
    start = timestamp() + 5
    assert tuple(details) == (1234, 1000, start, 180, 100_000, 2)
    assert tuple(surplus) == (view.amount_needed + 10**18, 50)
    # Expires right after the auction, like the SDK (start + duration + 12), within the TTL.
    traits = int(body["order"]["makerTraits"])
    assert (traits >> 80) & (2**40 - 1) == start + 180 + FUSION_EXPIRATION_DELAY == result.valid_to
    assert int(body["order"]["salt"]) >> 224 == 0xABA10994
    # The submitted pair validates on-chain through the Fusion adapter.
    order_tuple = tuple(
        int(body["order"][k])
        if k in ("salt", "makingAmount", "takingAmount", "makerTraits")
        else body["order"][k]
        for k in (
            "salt",
            "maker",
            "receiver",
            "makerAsset",
            "takerAsset",
            "makingAmount",
            "takingAmount",
            "makerTraits",
        )
    )
    signature = bytes.fromhex(body["signature"][2:])
    digest = order_digest_reference(order_tuple, publisher.domain_separator)
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE


def test_fusion_auction_is_clipped_to_the_ttl_or_skipped(
    client,
    auction,
    network_config,
    http,
    fusion_adapter,
    fusion_settlement_mock,
    oneinch_protocol,
    register,
    sell_token,
    lot,
    caplog,
):
    caplog.set_level(logging.INFO)
    register(fusion_adapter, oneinch_protocol)
    view = auction.lot_view(sell_token.address)
    http.on(
        "POST",
        "/quote/receive",
        lambda params, body: fusion_quote_for(
            fusion_settlement_mock.address, view.amount_needed, params, body
        ),
    )
    http.on("POST", "/order/submit", {"ok": True})
    short = network_config(order_ttl=100, oneinch=OneInchSettings(fusion_auction_duration=180))
    publisher = OneInchFusionPublisher(
        client, adapter_config(client, auction, fusion_adapter), short, http=http
    )
    result = publisher.publish(auction, view)
    assert result is not None
    assert (
        http.requests("POST", "/quote/receive")[0]["json"]["customPreset"]["auctionDuration"]
        == 100 - 12
    )
    assert result.valid_to == timestamp() + 100

    too_short = network_config(order_ttl=20, oneinch=OneInchSettings(fusion_auction_duration=180))
    publisher = OneInchFusionPublisher(
        client, adapter_config(client, auction, fusion_adapter), too_short, http=http
    )
    assert publisher.publish(auction, view) is None
    assert "not enough lot time" in caplog.text


def test_fusion_rejects_a_foreign_settlement_and_exclusive_resolver_delays(
    client,
    auction,
    network_config,
    http,
    fusion_adapter,
    fusion_settlement_mock,
    oneinch_protocol,
    register,
    sell_token,
    lot,
    caplog,
):
    caplog.set_level(logging.INFO)
    register(fusion_adapter, oneinch_protocol)
    view = auction.lot_view(sell_token.address)
    http.on(
        "POST",
        "/quote/receive",
        lambda params, body: fusion_quote_for("0x" + "34" * 20, view.amount_needed, params, body),
    )
    publisher = OneInchFusionPublisher(
        client, adapter_config(client, auction, fusion_adapter), network_config(), http=http
    )
    assert publisher.publish(auction, view) is None
    assert "is not the adapter's" in caplog.text and http.requests("POST", "/order/submit") == []

    # An exclusive resolver resolves from the start, the others from the auction start.
    quote = fusion_quote_for(
        fusion_settlement_mock.address,
        view.amount_needed,
        {"amount": "1"},
        {
            "customPreset": {
                "auctionDuration": 180,
                "auctionStartAmount": "2",
                "auctionEndAmount": "1",
            }
        },
        start_in=30,
    )
    quote["whitelist"] = ["0x" + "aa" * 20, "0x" + "bb" * 20]
    quote["presets"]["custom"]["exclusiveResolver"] = "0x" + "bb" * 20
    now = timestamp()
    extension = fusion_quote_extension(
        quote,
        quote["presets"]["custom"],
        now=now,
        auction_start=now + 30,
        taking_amount=1,
        custom_receiver=view.receiver,
    )
    post = extension[32:]
    bb_half = bytes.fromhex("bb" * 10)
    aa_half = bytes.fromhex("aa" * 10)
    # Post-interaction whitelist: resolving start now, bb first with delay 0, aa with delay 30.
    expected = (
        now.to_bytes(4, "big")
        + b"\x02"
        + bb_half
        + (0).to_bytes(2, "big")
        + aa_half
        + (30).to_bytes(2, "big")
    )
    assert expected in post
    assert boa.env.evm.patch.timestamp == now


# Config section


def test_config_section_defaults_and_parse(monkeypatch):
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    network = parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x")
    assert network.oneinch.enabled and network.oneinch.post
    assert network.oneinch.api_key_env == "ONEINCH_API_KEY"
    monkeypatch.delenv("ONEINCH_API_KEY", raising=False)
    assert network.oneinch.api_key is None

    monkeypatch.setenv("MY_1INCH", "k1")
    section = {
        "api_key_env": "MY_1INCH",
        "post": False,
        "fusion_auction_duration": 120,
        "orderbook_url": "https://private/orderbook",
    }
    network = parse_config(
        {"networks": {"x": {"rpc": "r", "auctions": [auction], "oneinch": section}}}
    ).network("x")
    assert network.oneinch.api_key == "k1" and not network.oneinch.post
    assert network.oneinch.fusion_auction_duration == 120
    assert network.oneinch.orderbook_url == "https://private/orderbook"
    for bad in (
        {"oneinch": {"nope": 1}},
        {"oneinch": {"post": "yes"}},
        {"oneinch": {"fusion_auction_duration": 0}},
        {"oneinch": "polygon"},
    ):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], **bad}}})


# Keeper cycle over the three 1inch rails


@pytest.fixture
def oneinch_rails(register, oneinch_adapter, fee_taker_adapter, fusion_adapter, oneinch_protocol):
    for adapter in (oneinch_adapter, fee_taker_adapter, fusion_adapter):
        register(adapter, oneinch_protocol)
    return {
        "OneInchAdapter": oneinch_adapter.address,
        "OneInchFeeTakerAdapter": fee_taker_adapter.address,
        "OneInchFusionAdapter": fusion_adapter.address,
    }


@pytest.fixture
def oneinch_feeds(http, harness, fee_taker_mock, fusion_settlement_mock, sell_token):
    """Canned fee-info, orderbook, quoter and relayer answers around the live lot."""

    def quote_now():
        return harness.getAmountNeeded(sell_token.address, harness.available(sell_token.address))

    http.on(
        "GET",
        "/fee-info",
        {
            "whitelist": {"a": "0x" + "aa" * 20},
            "feeBps": 100,
            "whitelistDiscountPercent": 50,
            "protocolFeeReceiver": "0x" + "77" * 20,
            "extensionAddress": fee_taker_mock.address,
            "source": "aba10994",
        },
    )
    http.on("POST", "/orderbook/v4.0/", {"success": True})
    http.on(
        "POST",
        "/quote/receive",
        lambda params, body: fusion_quote_for(
            fusion_settlement_mock.address, quote_now(), params, body
        ),
    )
    http.on("POST", "/order/submit", {"ok": True})
    return http


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_publishes_records_and_reposts(
    rail_keeper,
    network_config,
    oneinch_rails,
    oneinch_feeds,
    orderbook,
    sell_token,
    harness,
    lot,
    state,
    cow_adapter,
):
    config = network_config(
        **whitelisted(
            sell_token, poll_interval=60, order_ttl=300, oneinch=OneInchSettings(post=False)
        )
    )
    keeper = rail_keeper(config)
    assert {p.version for p in keeper.publishers_for(keeper.auctions[0])} == {
        "CowAdapter",
        *oneinch_rails,
    }

    actions = by_adapter(keeper.cycle())
    assert actions == {
        cow_adapter.address: "posted",
        **{address: "posted" for address in oneinch_rails.values()},
    }
    assert len(orderbook.posted) == 1
    assert (
        len(oneinch_feeds.requests("POST", "/orderbook/v4.0/")) == 1
    )  # FeeTaker; plain is log-only
    assert len(oneinch_feeds.requests("POST", "/order/submit")) == 1
    assert len(state.orders()) == 4
    for version, address in oneinch_rails.items():
        record = state.order("boa", harness.address, address, sell_token.address)
        assert record is not None, version
        assert record.lot_start == harness.window(sell_token.address)[0]
        assert record.sell_amount == harness.available(sell_token.address)

    # Within the validity every rail holds; expired, every rail reposts at the
    # fresh quote with a fresh salt.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    posts = oneinch_feeds.requests("POST", "/orderbook/v4.0/")
    assert len(posts) == 2
    assert int(posts[1]["json"]["data"]["takingAmount"]) < int(
        posts[0]["json"]["data"]["takingAmount"]
    )
    assert posts[1]["json"]["data"]["salt"] != posts[0]["json"]["data"]["salt"]


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, oneinch_rails, oneinch_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, oneinch=OneInchSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert oneinch_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, oneinch_rails, oneinch_feeds, sell_token, lot
):
    def boom(params, json):
        raise RuntimeError("fee-info exploded")

    oneinch_feeds.handlers.insert(0, ("GET", "/fee-info", boom))
    config = network_config(**whitelisted(sell_token, oneinch=OneInchSettings(post=False)))
    actions = by_adapter(rail_keeper(config).cycle())
    fee_taker = oneinch_rails["OneInchFeeTakerAdapter"]
    assert actions[fee_taker] == "failed"
    assert {action for adapter, action in actions.items() if adapter != fee_taker} == {"posted"}


def test_dry_run_posts_nothing(
    rail_keeper, network_config, oneinch_rails, oneinch_feeds, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token, oneinch=OneInchSettings(post=True)))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    # Only reads (fee-info and the Fusion quote) reach the feeds in a dry run.
    assert [c["url"].split("/")[-1] for c in oneinch_feeds.calls if c["method"] == "POST"] == [
        "receive"
    ]
