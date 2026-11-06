"""ZeroExSettlerPublisher against the real adapter (both flavors), the Settler
and Permit2 mocks and the auction's signature router; the Gasless API's
quote and submit calls are faked with the shapes of the 0x v2 docs, and the
keeper-mode self-relay settles through an executing Settler stand-in."""

import logging

import boa
import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    ERC1271_MAGIC_VALUE,
    ZEROX_ORDER_ABI,
    AdapterRegistry,
    Auction,
    ZeroExSettlerContract,
)
from keepers.auction_keeper.config import CurveRoute, TokenFilter, ZeroExSettings
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.zerox import (
    BASIC,
    BASIS,
    METATXN_TRANSFER_FROM,
    QUOTE_PATH,
    RAW_SIGNATURE_TYPE,
    SUBMIT_PATH,
    TRADE_TYPE,
    ZeroExOrder,
    ZeroExSettlerPublisher,
    curve_basic_action,
    hash_actions,
    transfer_from_action,
)
from tests.burners.test_zerox_module import hash_actions_reference, permit_digest_reference
from tests.keeper.conftest import LOT_AMOUNT, by_adapter, timestamp

PERMIT2_DOMAIN = keccak(b"PERMIT2_KEEPER_TEST_DOMAIN")
RELAY_DOMAIN = keccak(b"SETTLER_RELAY_KEEPER_TEST_DOMAIN")


@pytest.fixture(scope="module")
def permit2():
    return boa.load("contracts/testing/dutch_auction/Permit2Mock.vy", PERMIT2_DOMAIN)


# 0x deployer/registry features (ERC-721 token ids) of the two flavors.
FEATURE_METATXN = 3
FEATURE_INTENT = 4


@pytest.fixture(scope="module")
def settler_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/SettlerMock.vy")


@pytest.fixture(scope="module")
def settler_mock(settler_deployer):
    """A code-only Settler: the 0x adapter only requires code at the pinned address."""
    return settler_deployer.deploy()


@pytest.fixture(scope="module")
def settler_intent_mock(settler_deployer):
    """The Intent flavor's Settler: the registry maps an instance to one feature."""
    return settler_deployer.deploy()


@pytest.fixture(scope="module")
def settler_relay():
    """An executing Settler stand-in that is its own Permit2 (the executor)."""
    return boa.load("contracts/testing/dutch_auction/SettlerRelayMock.vy", RELAY_DOMAIN)


@pytest.fixture(scope="module")
def zerox_deployer(settler_mock, settler_intent_mock):
    """The 0x deployer/registry stand-in listing the mock Settlers the
    adapters pin: the MetaTxn Settler under feature 3, the Intent Settler
    under feature 4 (the registry maps an instance to one feature)."""
    deployer = boa.load("contracts/testing/dutch_auction/ZeroExRegistryMock.vy")
    deployer.add(FEATURE_METATXN, settler_mock)
    deployer.add(FEATURE_INTENT, settler_intent_mock)
    return deployer


@pytest.fixture(scope="module")
def relay_deployer(settler_relay):
    """A registry of its own for the executing relay mock, so the retirement
    tests move the MetaTxn Settler's feature without touching the relay."""
    deployer = boa.load("contracts/testing/dutch_auction/ZeroExRegistryMock.vy")
    deployer.add(FEATURE_METATXN, settler_relay)
    return deployer


@pytest.fixture
def zerox_adapter(settler_mock, zerox_deployer, permit2):
    """The SettlerMetaTxn-flavored 0x adapter."""
    return boa.load(
        "contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy",
        settler_mock,
        zerox_deployer,
        permit2,
        False,
    )


@pytest.fixture
def zerox_intent_adapter(settler_intent_mock, zerox_deployer, permit2):
    return boa.load(
        "contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy",
        settler_intent_mock,
        zerox_deployer,
        permit2,
        True,
    )


@pytest.fixture
def zerox_relay_adapter(settler_relay, relay_deployer):
    return boa.load(
        "contracts/burners/adapters/zerox/ZeroExSettlerAdapter.vy",
        settler_relay,
        relay_deployer,
        settler_relay,
        False,
    )


KEY_ENV = "ZEROX_API_KEY"
POOL = "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E"
ZID = "0x8f2d0a1b3c4d5e6f7a8b9c0d1e2f3a4b"
SUBMITTED = {"tradeHash": "0x" + "cd" * 32, "type": TRADE_TYPE, "zid": ZID}
# A fee action as the API appends them: BASIC transfers of the sell token.
FEE_ACTION = BASIC + bytes(160)


def gasless_quote(
    params: dict,
    *,
    settler: str,
    permit2: str,
    chain_id: int,
    min_amount_out: int,
    deadline: int,
    nonce: int = 2241959297937691820908574931991596,
    domain_separator: bytes = PERMIT2_DOMAIN,
    liquidity: bool = True,
    trade_hash: str | None = None,
    **message_overrides,
) -> dict:
    """A ``/gasless/quote`` answer for ``params`` in the documented shape: the
    ``settler_metatransaction`` trade with its Permit2 ``eip712`` payload
    (amounts and the nonce as decimal strings) and ``hash``."""
    if not liquidity:
        return {"liquidityAvailable": False, "zid": ZID}
    permit = (params["sellToken"], int(params["sellAmount"]), nonce, deadline)
    actions = [transfer_from_action(settler, permit), FEE_ACTION]
    message = {
        "permitted": {"token": params["sellToken"].lower(), "amount": params["sellAmount"]},
        "spender": settler.lower(),
        "nonce": str(nonce),
        "deadline": str(deadline),
        "slippageAndActions": {
            "recipient": params["recipient"].lower(),
            "buyToken": params["buyToken"].lower(),
            "minAmountOut": str(min_amount_out),
            "actions": ["0x" + a.hex() for a in actions],
        },
    }
    message.update(message_overrides)
    order = ZeroExOrder.from_message(message, hash_actions_reference(actions))
    digest = permit_digest_reference(order.as_tuple(), settler, domain_separator, False)
    return {
        "allowanceTarget": permit2.lower(),
        "approval": None,
        "blockNumber": "1",
        "buyAmount": str(min_amount_out + 10**16),
        "buyToken": params["buyToken"].lower(),
        "fees": {"integratorFee": None, "zeroExFee": None, "gasFee": None},
        "issues": {"allowance": None, "balance": None, "simulationIncomplete": False},
        "liquidityAvailable": True,
        "minBuyAmount": str(min_amount_out),
        "sellAmount": params["sellAmount"],
        "sellToken": params["sellToken"].lower(),
        "target": settler.lower(),
        "trade": {
            "type": TRADE_TYPE,
            "hash": trade_hash or "0x" + digest.hex(),
            "eip712": {
                "types": {
                    "PermitWitnessTransferFrom": [
                        {"name": "permitted", "type": "TokenPermissions"},
                        {"name": "spender", "type": "address"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "deadline", "type": "uint256"},
                        {"name": "slippageAndActions", "type": "SlippageAndActions"},
                    ],
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "SlippageAndActions": [
                        {"name": "recipient", "type": "address"},
                        {"name": "buyToken", "type": "address"},
                        {"name": "minAmountOut", "type": "uint256"},
                        {"name": "actions", "type": "bytes[]"},
                    ],
                    "TokenPermissions": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint256"},
                    ],
                },
                "domain": {
                    "name": "Permit2",
                    "chainId": chain_id,
                    "verifyingContract": permit2.lower(),
                },
                "primaryType": "PermitWitnessTransferFrom",
                "message": message,
            },
        },
        "zid": ZID,
    }


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv(KEY_ENV, "key-0x")


@pytest.fixture
def registered(client, auction, register):
    def _registered(adapter, executor):
        register(adapter, executor)
        registry = AdapterRegistry(client, auction.registry())
        return next(a for a in registry.active_adapters() if a.adapter == adapter.address)

    return _registered


@pytest.fixture
def adapter(registered, zerox_adapter, permit2):
    return registered(zerox_adapter, permit2)


@pytest.fixture
def quote_for(settler_mock, permit2, client):
    def _quote(view, **overrides):
        def _build(params, json):
            kwargs = {
                "settler": settler_mock.address,
                "permit2": permit2.address,
                "chain_id": client.chain_id,
                "min_amount_out": view.amount_needed + 10**18,
                "deadline": timestamp() + 300,
            }
            kwargs.update(overrides)
            return gasless_quote(params, **kwargs)

        return _build

    return _quote


@pytest.fixture
def route(sell_token):
    return CurveRoute(token=sell_token.address, pool=POOL, i=0, j=1)


def _to_tuple(order) -> tuple:
    permit, slippage, actions_hash = order
    return (tuple(permit), tuple(slippage), bytes(actions_hash))


# Gasless API


def test_gasless_quote_is_checked_signed_and_submitted(
    client,
    auction,
    harness,
    key,
    http,
    adapter,
    zerox_adapter,
    settler_mock,
    permit2,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    quote_for,
    network_config,
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", SUBMIT_PATH, SUBMITTED)
    publisher = ZeroExSettlerPublisher(client, adapter, network_config(), http=http)
    assert publisher.settler == settler_mock.address and publisher.permit2 == permit2.address
    assert publisher.domain_separator == PERMIT2_DOMAIN and not publisher.intent
    assert publisher.feed_ready

    result = publisher.publish(auction, view)

    assert result is not None and result.details["mode"] == "gasless-api"
    (quote_call,) = http.requests("GET", QUOTE_PATH)
    assert quote_call["url"] == "https://api.0x.org" + QUOTE_PATH
    assert quote_call["params"] == {
        "chainId": str(client.chain_id),
        "sellToken": sell_token.address,
        "buyToken": want.address,
        "sellAmount": str(LOT_AMOUNT),
        "taker": harness.address,
        "recipient": proceeds_receiver,
    }
    assert quote_call["headers"] == {"0x-api-key": "key-0x", "0x-version": "v2"}
    (submit_call,) = http.requests("POST", SUBMIT_PATH)
    assert submit_call["headers"] == quote_call["headers"]
    body = submit_call["json"]
    assert set(body) == {"chainId", "trade"} and body["chainId"] == client.chain_id
    assert set(body["trade"]) == {"type", "eip712", "signature"}
    assert body["trade"]["type"] == TRADE_TYPE
    quote = quote_for(view)(quote_call["params"], None)
    assert body["trade"]["eip712"] == quote["trade"]["eip712"]  # verbatim, as the docs ask
    assert body["trade"]["signature"] == {
        "signatureType": RAW_SIGNATURE_TYPE,
        "signatureBytes": result.details["signature"],
    }
    # The signature is adapter ++ abi_encode(order) with the quote's permit,
    # slippage and the Settler hash of the quote's actions.
    message = quote["trade"]["eip712"]["message"]
    actions = [bytes.fromhex(a[2:]) for a in message["slippageAndActions"]["actions"]]
    order = ZeroExOrder.from_message(message, hash_actions(actions))
    assert order.amount == LOT_AMOUNT == result.sell_amount
    assert order.min_amount_out == view.amount_needed + 10**18 == result.buy_amount
    assert order.deadline == timestamp() + 300 == result.valid_to
    assert order.recipient == proceeds_receiver and order.buy_token == want.address
    signature = bytes.fromhex(result.details["signature"][2:])
    assert signature == bytes.fromhex(zerox_adapter.address[2:]) + encode(
        [ZEROX_ORDER_ABI], [order.as_tuple()]
    )
    assert len(signature) == 20 + 8 * 32
    digest = bytes.fromhex(quote["trade"]["hash"][2:])
    assert order.permit_digest(settler_mock.address, PERMIT2_DOMAIN, False) == digest
    _, described = zerox_adapter.describe(signature[20:])
    assert bytes(described) == digest
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.details["tradeHash"] == quote["trade"]["hash"]
    assert result.uid == SUBMITTED["tradeHash"]  # the backend's id wins once submitted
    assert result.details["zid"] == ZID and result.details["response"] == SUBMITTED
    assert result.details["actions"] == message["slippageAndActions"]["actions"]


def test_quote_below_the_curve_or_for_another_lot_is_skipped(
    client,
    auction,
    key,
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
    publisher = ZeroExSettlerPublisher(
        client, adapter, network_config(zerox=ZeroExSettings(relay="none")), http=http
    )
    foreign = "0x" + "12" * 20
    cases = [
        (dict(min_amount_out=view.amount_needed - 1), "is below the curve quote"),
        (dict(spender=foreign), "is not the pinned Settler"),
        (dict(permit2=foreign), "is not the adapter's Permit2"),
        (dict(chain_id=999), "is not"),
        (
            dict(permitted={"token": other_token.address, "amount": str(LOT_AMOUNT)}),
            "not the lot pair",
        ),
        (
            dict(permitted={"token": sell_token.address, "amount": str(LOT_AMOUNT + 1)}),
            "exceeds the available",
        ),
        (dict(deadline=view.end + 1), "past the lot end"),
        (dict(deadline=timestamp()), "already expired"),
        (dict(trade_hash="0x" + "ee" * 32), "differs from the adapter's digest"),
        (dict(domain_separator=keccak(b"other")), "differs from the adapter's digest"),
        (dict(liquidity=False), "no liquidity"),
    ]
    for kwargs, reason in cases:
        http.handlers.clear()
        http.on("GET", QUOTE_PATH, quote_for(view, **kwargs))
        assert publisher.publish(auction, view) is None, reason
        assert reason in caplog.text, reason
    # A recipient other than the FeeCollector is refused too.
    http.handlers.clear()
    http.on(
        "GET",
        QUOTE_PATH,
        quote_for(
            view,
            slippageAndActions={
                "recipient": foreign,
                "buyToken": view.want,
                "minAmountOut": str(view.amount_needed),
                "actions": ["0x" + FEE_ACTION.hex()],
            },
        ),
    )
    assert publisher.publish(auction, view) is None
    assert "is not the auction's receiver" in caplog.text
    assert http.requests("POST", SUBMIT_PATH) == []
    # Exactly the curve is fine.
    http.handlers.clear()
    http.on("GET", QUOTE_PATH, quote_for(view, min_amount_out=view.amount_needed))
    http.on("POST", SUBMIT_PATH, SUBMITTED)
    assert publisher.publish(auction, view) is not None


def test_api_errors_are_logged_not_raised(
    client, auction, key, http, adapter, sell_token, lot, quote_for, network_config, caplog
):
    view = auction.lot_view(sell_token.address)
    publisher = ZeroExSettlerPublisher(
        client, adapter, network_config(zerox=ZeroExSettings(relay="none")), http=http
    )

    def unauthorized(params, json):
        raise ApiError(401, {"name": "UNAUTHORIZED", "message": "invalid api key"}, QUOTE_PATH)

    http.on("GET", QUOTE_PATH, unauthorized)
    assert publisher.publish(auction, view) is None
    assert "invalid api key" in caplog.text

    def invalid_signer(params, json):
        raise ApiError(400, {"name": "INVALID_SIGNER", "message": "bad signer"}, SUBMIT_PATH)

    http.handlers.clear()
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", SUBMIT_PATH, invalid_signer)
    assert publisher.publish(auction, view) is None
    assert "INVALID_SIGNER" in caplog.text


def test_unsignable_quote_falls_back_to_the_self_relay(
    client, auction, key, http, adapter, sell_token, lot, quote_for, route, network_config
):
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view, min_amount_out=view.amount_needed - 1))
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        network_config(zerox=ZeroExSettings(relay="log", routes=(route,))),
        http=http,
    )
    result = publisher.publish(auction, view)
    assert result is not None and result.details["mode"] == "self-relay"
    assert http.requests("POST", SUBMIT_PATH) == []


def test_without_a_key_or_with_post_off_no_request_is_made(
    client, auction, http, adapter, sell_token, lot, route, network_config, monkeypatch, caplog
):
    monkeypatch.delenv(KEY_ENV, raising=False)
    view = auction.lot_view(sell_token.address)
    publisher = ZeroExSettlerPublisher(
        client, adapter, network_config(zerox=ZeroExSettings(routes=(route,))), http=http
    )
    assert not publisher.feed_ready and "no Gasless API key" in caplog.text
    assert publisher.publish(auction, view) is not None and http.calls == []
    monkeypatch.setenv(KEY_ENV, "k")
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        network_config(zerox=ZeroExSettings(post=False, routes=(route,))),
        http=http,
    )
    assert not publisher.feed_ready
    assert publisher.publish(auction, view) is not None and http.calls == []


# Self-relay


def test_self_relay_builds_a_curve_fill_and_logs_the_calldata(
    client,
    auction,
    harness,
    http,
    adapter,
    zerox_adapter,
    settler_mock,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    route,
    network_config,
    caplog,
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    config = network_config(
        order_ttl=10**6, zerox=ZeroExSettings(relay="self-actions", routes=(route,))
    )
    publisher = ZeroExSettlerPublisher(client, adapter, config, http=http)
    result = publisher.publish(auction, view)
    assert result is not None and http.calls == []  # offline mode: nothing sent, nothing posted
    assert result.details["mode"] == "self-relay" and result.details["relay"] == "self-actions"
    assert "not relayed" in caplog.text and "txHash" not in result.details

    calldata = bytes.fromhex(result.details["calldata"][2:])
    settler = ZeroExSettlerContract(client, settler_mock.address)
    assert calldata[:4] == settler.EXECUTE_META_TXN.selector
    slippage, actions, zid, msg_sender, sig = decode(settler.EXECUTE_META_TXN.inputs, calldata[4:])
    assert msg_sender.lower() == harness.address.lower() and zid == bytes(32)
    assert sig == bytes.fromhex(result.details["signature"][2:])
    assert [("0x" + a.hex()) for a in actions] == result.details["actions"]
    # The action list of the fork test: pull the lot into the Settler, swap it
    # on the configured Curve pool, the Settler's slippage check pays the rest.
    assert METATXN_TRANSFER_FROM == bytes.fromhex("0dfeb419") and BASIC == bytes.fromhex("38c9c147")
    order = ZeroExOrder.from_tuple(decode([ZEROX_ORDER_ABI], sig[20:])[0])
    assert actions[0] == transfer_from_action(settler_mock.address, order.permit)
    assert actions[0] == METATXN_TRANSFER_FROM + encode(
        ["address", "((address,uint256),uint256,uint256)"],
        [settler_mock.address, ((sell_token.address, LOT_AMOUNT), order.nonce, view.end)],
    )
    exchange = keccak(text="exchange(int128,int128,uint256,uint256)")[:4]
    assert (
        actions[1]
        == curve_basic_action(route)
        == BASIC
        + encode(
            ["address", "uint256", "address", "uint256", "bytes"],
            [
                sell_token.address,
                BASIS,
                POOL,
                4 + 2 * 32,
                exchange + encode(["int128", "int128", "uint256", "uint256"], [0, 1, 0, 0]),
            ],
        )
    )
    assert BASIS == 1_000_000
    # The order is order_for's, bound to the actions through the adapter's hash.
    assert order.actions_hash == bytes(zerox_adapter.hash_actions(list(actions)))
    assert (
        order.actions_hash == hash_actions(list(actions)) == hash_actions_reference(list(actions))
    )
    expected, _ = zerox_adapter.order_for(
        harness.address, sell_token.address, order.nonce, 0, view.end, order.actions_hash
    )
    assert _to_tuple(expected) == order.as_tuple()
    assert (tuple(slippage[:2]), slippage[2]) == (
        (proceeds_receiver.lower(), want.address.lower()),
        view.amount_needed,
    )
    assert order.deadline == view.end == result.valid_to  # the TTL is longer than the lot
    assert result.sell_amount == LOT_AMOUNT and result.buy_amount == view.amount_needed
    digest = permit_digest_reference(order.as_tuple(), settler_mock.address, PERMIT2_DOMAIN, False)
    assert result.uid == "0x" + digest.hex()
    assert harness.isValidSignature(digest, sig) == ERC1271_MAGIC_VALUE
    # Fresh nonces, the TTL shortening the deadline.
    publisher = ZeroExSettlerPublisher(
        client, adapter, network_config(order_ttl=120, zerox=config.zerox), http=http
    )
    first, second = publisher.publish(auction, view), publisher.publish(auction, view)
    assert first is not None and second is not None and first.uid != second.uid
    assert first.details["order"]["permit"]["nonce"] != second.details["order"]["permit"]["nonce"]
    assert first.valid_to == second.valid_to == timestamp() + 120


def test_relay_none_or_no_route_publishes_nothing(
    client, auction, http, adapter, sell_token, other_token, lot, route, network_config, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        network_config(zerox=ZeroExSettings(relay="none", routes=(route,))),
        http=http,
    )
    assert publisher.publish(auction, view) is None and "relay is none" in caplog.text
    publisher = ZeroExSettlerPublisher(client, adapter, network_config(), http=http)
    assert publisher.publish(auction, view) is None and "no Curve route" in caplog.text
    assert publisher.publish(auction, auction.lot_view(other_token.address)) is None
    assert http.calls == []


def test_keeper_mode_relay_log_sends_only_the_allowance_sync(
    harness,
    registered,
    zerox_adapter,
    permit2,
    sell_token,
    lot,
    route,
    keeper_account,
    http,
    network_config,
):
    adapter = registered(zerox_adapter, permit2)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        keeper_config(network_config, ZeroExSettings(relay="log", routes=(route,))),
        http=http,
    )
    assert sell_token.allowance(harness.address, permit2.address) == 0
    result = publisher.publish(auction, auction.lot_view(sell_token.address))
    assert result is not None and "txHash" not in result.details
    assert sell_token.allowance(harness.address, permit2.address) == 2**256 - 1
    assert len(client.sent) == 1


def keeper_config(network_config, zerox: ZeroExSettings):
    return network_config(mode="keeper", keeper_key_env="KEY", zerox=zerox)


def test_keeper_mode_self_actions_settles_through_the_settler(
    harness,
    registered,
    zerox_relay_adapter,
    settler_relay,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    route,
    keeper_account,
    http,
    caplog,
    network_config,
):
    caplog.set_level(logging.INFO)
    adapter = registered(zerox_relay_adapter, settler_relay)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    view = auction.lot_view(sell_token.address)
    # The stand-in holds the buy token the swap would have produced: the
    # curve quote plus a surplus, all of which goes to the recipient.
    payout = view.amount_needed + 7 * 10**18
    want._mint_for_testing(settler_relay.address, payout)
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        keeper_config(network_config, ZeroExSettings(relay="self-actions", routes=(route,))),
        http=http,
    )
    assert publisher.settler == publisher.permit2 == settler_relay.address
    assert publisher.domain_separator == RELAY_DOMAIN

    result = publisher.publish(auction, view)

    assert result is not None and "txHash" in result.details and "relayed 0x MetaTxn" in caplog.text
    assert [to for to, _ in client.sent] == [harness.address, settler_relay.address]
    assert client.sent[1][1] == bytes.fromhex(result.details["calldata"][2:])
    assert settler_relay.relays() == 1
    assert sell_token.balanceOf(harness.address) == 0 and harness.available(sell_token.address) == 0
    assert sell_token.balanceOf(settler_relay.address) == LOT_AMOUNT
    assert want.balanceOf(proceeds_receiver) == payout
    nonce = int(result.details["order"]["permit"]["nonce"])
    assert settler_relay.used_nonces(
        keccak(encode(["address", "uint256"], [harness.address, nonce]))
    )
    assert result.sell_amount == LOT_AMOUNT and result.buy_amount == view.amount_needed
    # The lot is gone: nothing more to publish.
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is None


def test_keeper_mode_relay_revert_is_logged_and_nothing_sent(
    harness,
    registered,
    zerox_relay_adapter,
    settler_relay,
    sell_token,
    lot,
    route,
    keeper_account,
    http,
    caplog,
    network_config,
):
    adapter = registered(zerox_relay_adapter, settler_relay)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        keeper_config(network_config, ZeroExSettings(relay="self-actions", routes=(route,))),
        http=http,
    )
    # Unfunded stand-in: the Settler's slippage check fails in simulation.
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is None
    assert "executeMetaTxn reverted" in caplog.text and "slippage" in caplog.text
    assert [to for to, _ in client.sent] == [harness.address]  # the allowance sync only
    assert settler_relay.relays() == 0 and sell_token.balanceOf(harness.address) == LOT_AMOUNT


def test_dry_run_posts_and_sends_nothing(
    harness,
    registered,
    zerox_adapter,
    permit2,
    key,
    sell_token,
    lot,
    route,
    keeper_account,
    http,
    quote_for,
    network_config,
):
    adapter = registered(zerox_adapter, permit2)
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        keeper_config(network_config, ZeroExSettings(relay="self-actions", routes=(route,))),
        http=http,
    )
    result = publisher.publish(auction, view, dry_run=True)
    assert result is not None and result.details["mode"] == "gasless-api"
    assert http.requests("POST", SUBMIT_PATH) == [] and client.sent == []
    http.handlers.clear()
    http.on("GET", QUOTE_PATH, quote_for(view, liquidity=False))
    result = publisher.publish(auction, view, dry_run=True)
    assert result is not None and result.details["mode"] == "self-relay"
    assert client.sent == [] and "txHash" not in result.details


# Intent


def test_intent_adapter_publishes_standing_asks_for_solvers(
    client,
    auction,
    harness,
    key,
    http,
    registered,
    zerox_intent_adapter,
    settler_intent_mock,
    permit2,
    sell_token,
    want,
    proceeds_receiver,
    lot,
    route,
    network_config,
    caplog,
):
    caplog.set_level(logging.INFO)
    adapter = registered(zerox_intent_adapter, permit2)
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, lambda p, j: pytest.fail("the API serves MetaTxn trades only"))
    publisher = ZeroExSettlerPublisher(
        client,
        adapter,
        network_config(
            order_ttl=10**6, zerox=ZeroExSettings(relay="self-actions", routes=(route,))
        ),
        http=http,
    )
    assert publisher.intent and not publisher.feed_ready
    result = publisher.publish(auction, view)
    assert result is not None and http.calls == []
    assert result.details["mode"] == "intent" and "built for solvers" in caplog.text
    signature = bytes.fromhex(result.details["signature"][2:])
    order = ZeroExOrder.from_tuple(decode([ZEROX_ORDER_ABI], signature[20:])[0])
    assert order.actions_hash == bytes(32)
    assert order.permit[:2] == (sell_token.address, LOT_AMOUNT)
    assert order.slippage == (proceeds_receiver, want.address, view.amount_needed)
    assert order.deadline == view.end == result.valid_to
    expected, _ = zerox_intent_adapter.order_for(harness.address, sell_token.address, order.nonce)
    assert _to_tuple(expected) == order.as_tuple()
    digest = permit_digest_reference(
        order.as_tuple(), settler_intent_mock.address, PERMIT2_DOMAIN, True
    )
    assert result.uid == "0x" + digest.hex()
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    # The MetaTxn witness does not validate for an intent order and vice versa.
    other = permit_digest_reference(
        order.as_tuple(), settler_intent_mock.address, PERMIT2_DOMAIN, False
    )
    assert publisher.validate_signature(auction, other, signature) == "OrderNotValid('InvalidHash')"
    second = publisher.publish(auction, view)
    assert second is not None and second.uid != result.uid


def test_flavor_override_warns_and_underpriced_orders_are_rejected(
    client,
    auction,
    harness,
    adapter,
    zerox_adapter,
    settler_mock,
    sell_token,
    lot,
    network_config,
    caplog,
):
    publisher = ZeroExSettlerPublisher(
        client, adapter, network_config(zerox=ZeroExSettings(flavor="intent")), http=None
    )
    assert publisher.intent and "overrides the pinned flavor" in caplog.text
    publisher = ZeroExSettlerPublisher(client, adapter, network_config(), http=None)
    assert not publisher.intent
    built, _ = zerox_adapter.order_for(
        harness.address, sell_token.address, 5, 0, 0, keccak(b"actions")
    )
    order = ZeroExOrder.from_tuple(_to_tuple(built))
    cheap = ZeroExOrder(**{**order.__dict__, "min_amount_out": order.min_amount_out - 1})
    reason = publisher.validate_signature(
        auction,
        cheap.permit_digest(settler_mock.address, PERMIT2_DOMAIN, False),
        cheap.signature(zerox_adapter.address),
    )
    assert reason == "BadBuyAmount()"


# Config section


def test_config_section_defaults_parse_and_routes(monkeypatch):
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    token_a, token_b = "0x" + "aa" * 20, "0x" + "bb" * 20
    zerox = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x").zerox
    )
    assert zerox.enabled and zerox.post and zerox.api_key_env == KEY_ENV
    assert zerox.api_url == "https://api.0x.org" and zerox.chain_id is None
    assert zerox.flavor is None and zerox.relay == "log" and zerox.slippage_ppm is None
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert zerox.routes == () and zerox.api_key is None

    monkeypatch.setenv("MY_0X", "k0")
    section = {
        "api_key_env": "MY_0X",
        "post": False,
        "chain_id": 137,
        "flavor": "intent",
        "relay": "self-actions",
        "slippage_ppm": 0,
        "routes": [{"token": token_a.lower(), "pool": token_b, "i": 0, "j": 1}],
    }
    zerox = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], "zerox": section}}})
        .network("x")
        .zerox
    )
    assert zerox.api_key == "k0" and not zerox.post and zerox.chain_id == 137
    assert zerox.flavor == "intent" and zerox.relay == "self-actions" and zerox.slippage_ppm == 0
    (route,) = zerox.routes
    assert (route.token.lower(), route.pool.lower(), route.i, route.j) == (token_a, token_b, 0, 1)
    # Nullable integers may be given as null.
    zerox = (
        parse_config(
            {
                "networks": {
                    "x": {
                        "rpc": "r",
                        "auctions": [auction],
                        "zerox": {"chain_id": None, "slippage_ppm": None},
                    }
                }
            }
        )
        .network("x")
        .zerox
    )
    assert zerox.chain_id is None and zerox.slippage_ppm is None

    for bad in (
        {"relay": "auto"},
        {"flavor": "metatx"},
        {"chain_id": 0},
        {"slippage_ppm": -1},
        {"routes": {"token": "0x"}},
        {"routes": [{"token": "0x1", "pool": "0x2", "i": 0, "j": 1}]},
        {"routes": [{"token": token_a, "pool": token_b, "i": -1, "j": 1}]},
        {"routes": [{"token": token_a, "pool": token_b, "i": 0}]},
        {
            "routes": [
                {"token": token_a, "pool": token_b, "i": 0, "j": 1},
                {"token": token_a, "pool": "0x" + "cc" * 20, "i": 1, "j": 0},
            ]
        },
        "polygon",
    ):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], "zerox": bad}}})


# Keeper cycle over the 0x rail (Gasless API path)


@pytest.fixture
def zerox_rail(register, zerox_adapter, permit2, key):
    register(zerox_adapter, permit2)
    return zerox_adapter.address


@pytest.fixture
def zerox_feeds(http, harness, settler_mock, permit2, client, sell_token):
    """A Gasless quote at the live curve price, valid for 300 s, and the submit answer."""

    def quote_now():
        return harness.getAmountNeeded(sell_token.address, harness.available(sell_token.address))

    http.on(
        "GET",
        QUOTE_PATH,
        lambda params, body: gasless_quote(
            params,
            settler=settler_mock.address,
            permit2=permit2.address,
            chain_id=client.chain_id,
            min_amount_out=quote_now(),
            deadline=timestamp() + 300,
        ),
    )
    http.on("POST", SUBMIT_PATH, SUBMITTED)
    return http


def whitelisted(sell_token, **overrides):
    return dict(tokens=TokenFilter(whitelist=frozenset([sell_token.address])), **overrides)


def test_keeper_cycle_publishes_records_and_reposts(
    rail_keeper,
    network_config,
    zerox_rail,
    zerox_feeds,
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
        "ZeroExSettlerAdapter",
    }

    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", zerox_rail: "posted"}
    assert len(orderbook.posted) == 1 and len(zerox_feeds.requests("POST", SUBMIT_PATH)) == 1
    record = state.order("boa", harness.address, zerox_rail, sell_token.address)
    assert record is not None
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.sell_amount == harness.available(sell_token.address)
    assert record.valid_to == timestamp() + 300  # the quote's permit deadline

    # Within the validity both rails hold; expired, a fresh quote is signed
    # and submitted at the lower curve price.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    submits = zerox_feeds.requests("POST", SUBMIT_PATH)
    assert len(submits) == 2
    floors = [
        int(s["json"]["trade"]["eip712"]["message"]["slippageAndActions"]["minAmountOut"])
        for s in submits
    ]
    assert floors[1] < floors[0]
    assert all(
        s["json"]["trade"]["signature"]["signatureType"] == RAW_SIGNATURE_TYPE for s in submits
    )


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, zerox_rail, zerox_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**whitelisted(sell_token, zerox=ZeroExSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert zerox_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, zerox_rail, zerox_feeds, sell_token, lot, cow_adapter
):
    def boom(params, json):
        raise RuntimeError("quote exploded")

    zerox_feeds.handlers.insert(0, ("GET", QUOTE_PATH, boom))
    config = network_config(**whitelisted(sell_token))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        zerox_rail: "failed",
    }


def test_dry_run_posts_nothing(
    rail_keeper, network_config, zerox_rail, zerox_feeds, orderbook, sell_token, lot, state
):
    config = network_config(**whitelisted(sell_token))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    assert [c["method"] for c in zerox_feeds.calls] == ["GET"]


# 0x's pause: the publisher follows the registry and stops for a retired Settler


def test_retired_settler_stops_the_publisher_for_good(
    client,
    auction,
    harness,
    key,
    http,
    adapter,
    zerox_adapter,
    zerox_deployer,
    settler_deployer,
    sell_token,
    lot,
    quote_for,
    network_config,
    caplog,
):
    caplog.set_level(logging.INFO)
    publisher = ZeroExSettlerPublisher(client, adapter, network_config(), http=http)
    assert publisher.registry.address == zerox_deployer.address
    assert publisher.feature == FEATURE_METATXN and not publisher.retired
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", SUBMIT_PATH, SUBMITTED)

    # Current, then previous (one newer deployment): still served.
    assert publisher.publish(auction, view) is not None
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    assert zerox_deployer.prev(FEATURE_METATXN) == zerox_adapter.settler()
    assert publisher.publish(auction, view) is not None
    assert len(http.requests("GET", QUOTE_PATH)) == 2

    # Two newer deployments: retired. The registry is asked before the quote,
    # nothing is requested, the error is logged once, later polls are silent.
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    assert publisher.publish(auction, view) is None
    assert publisher.retired and len(http.requests("GET", QUOTE_PATH)) == 2
    assert caplog.text.count("is retired") == 1
    assert publisher.publish(auction, view) is None
    assert caplog.text.count("is retired") == 1


def test_settler_retired_between_the_registry_check_and_validation_is_terminal(
    client,
    auction,
    key,
    http,
    adapter,
    zerox_deployer,
    settler_deployer,
    sell_token,
    lot,
    quote_for,
    network_config,
    caplog,
):
    """0x removes the Settler after the registry check passed: the router's
    OrderNotValid(SettlerRetired) answer retires the publisher, unlike an
    economic reject, which only skips this poll."""
    caplog.set_level(logging.WARNING)
    publisher = ZeroExSettlerPublisher(client, adapter, network_config(), http=http)
    view = auction.lot_view(sell_token.address)
    http.on("GET", QUOTE_PATH, quote_for(view))
    http.on("POST", SUBMIT_PATH, SUBMITTED)
    publisher.registry.lists = lambda feature, settler: True  # the race: still listed
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())

    assert publisher.publish(auction, view) is None
    assert publisher.retired
    assert len(http.requests("GET", QUOTE_PATH)) == 1 and http.requests("POST", SUBMIT_PATH) == []
    assert "SettlerRetired" in caplog.text and "is retired" in caplog.text
    assert publisher.publish(auction, view) is None
    assert len(http.requests("GET", QUOTE_PATH)) == 1


def test_keeper_cycle_skips_a_retired_adapter_without_failing_the_rest(
    rail_keeper,
    network_config,
    zerox_rail,
    zerox_feeds,
    zerox_deployer,
    settler_deployer,
    sell_token,
    lot,
    cow_adapter,
):
    config = network_config(**whitelisted(sell_token))
    keeper = rail_keeper(config)
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    zerox_deployer.add(FEATURE_METATXN, settler_deployer.deploy())
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", zerox_rail: "skipped"}
    assert zerox_feeds.calls == []
