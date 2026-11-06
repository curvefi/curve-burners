"""HashflowPublisher against the real adapter, the router and pool mocks and
the auction's signature router; the RFQ call is faked with a response shaped
from the taker API v3 docs (``quotes[{quoteData, signature}]``), the maker
signing the pool's EIP-191 digest with the test key."""

import logging

import boa
import pytest
from eth_keys import keys
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import ERC1271_MAGIC_VALUE, AdapterRegistry, Auction
from keepers.auction_keeper.config import HashflowSettings, TokenFilter
from keepers.auction_keeper.rails.common import ApiError
from keepers.auction_keeper.rails.hashflow import (
    RFQ_PATH,
    RFQ_TYPE_RFQM,
    HashflowPublisher,
    HashflowQuote,
)
from tests.burners.adapters.hashflow.test_module import quote_digest_reference, sign_maker
from tests.keeper.conftest import LOT_AMOUNT, WAD, by_adapter, timestamp

# The Hashflow market maker's key: the pool recovers quotes to its address.
HASHFLOW_MAKER_KEY = keys.PrivateKey(keccak(b"keeper hashflow market maker"))
OTHER_KEY_SEED = b"not the hashflow market maker"


def _forget_stale_storage_trace(contract) -> None:
    """boa artifact (see tests/burners/adapters/hashflow/test_adapter.py): anchor()
    rolls the chain back but not boa's global sstore trace, so a contract
    deployed at an address another suite used inherits foreign slots in the
    storage dump boa renders for a revert of this contract."""
    from boa.util.abi import Address

    boa.env.sstore_trace.pop(Address(contract.address), None)


@pytest.fixture(scope="module")
def hashflow_router():
    router = boa.load("contracts/testing/dutch_auction/HashflowRouterMock.vy", False)
    _forget_stale_storage_trace(router)
    return router


@pytest.fixture(scope="module")
def hashflow_pool(hashflow_router):
    pool = boa.load(
        "contracts/testing/dutch_auction/HashflowPoolMock.vy",
        HASHFLOW_MAKER_KEY.public_key.to_checksum_address(),
        hashflow_router,
    )
    _forget_stale_storage_trace(pool)
    hashflow_router.updatePoolAuthorization(pool.address, True)
    return pool


@pytest.fixture
def hashflow_adapter(hashflow_router):
    return boa.load("contracts/burners/adapters/hashflow/HashflowAdapter.vy", hashflow_router)


RFQ_ID = "0f9a3b9c-7a1e-4b6e-9d2a-2f5d1b9a0c11"


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


@pytest.fixture
def adapter(client, auction, hashflow_adapter, hashflow_router, register):
    register(hashflow_adapter, hashflow_router.address)
    registry = AdapterRegistry(client, auction.registry())
    return next(a for a in registry.active_adapters() if a.adapter == hashflow_adapter.address)


@pytest.fixture
def credential(monkeypatch):
    monkeypatch.setenv("HASHFLOW_API_KEY", "hashflow-credential")


@pytest.fixture
def pool_liquidity(want, hashflow_pool):
    want._mint_for_testing(hashflow_pool.address, 1_000_000 * WAD)


@pytest.fixture
def rfq_for(hashflow_pool, hashflow_router):
    """A successful RFQ answer for the lot in the docs' shape: one RFQ-m quote
    with the maker's signature over the pool digest."""

    def _rfq(
        view,
        *,
        quote_amount=None,
        expiry=None,
        base_amount=None,
        maker_key=HASHFLOW_MAKER_KEY,
        signature=None,
        **quote_data_overrides,
    ):
        base_amount = view.available if base_amount is None else base_amount
        quote_amount = view.amount_needed + WAD if quote_amount is None else quote_amount
        expiry = timestamp() + 60 if expiry is None else expiry
        txid = keccak(f"txid-{timestamp()}-{quote_amount}".encode())
        chain = {"chainType": "evm", "chainId": boa.env.evm.patch.chain_id}
        quote_data = {
            "rfqType": RFQ_TYPE_RFQM,
            "baseChain": chain,
            "quoteChain": chain,
            "baseToken": view.token,
            "baseTokenAmount": str(base_amount),
            "quoteToken": view.want,
            "quoteTokenAmount": str(quote_amount),
            "trader": view.auction,
            "effectiveTrader": None,
            "txid": "0x" + txid.hex(),
            "pool": hashflow_pool.address,
            "dstPool": None,
            "quoteExpiry": expiry,
            "nonce": None,
            "externalAccount": None,
            "dstExternalAccount": None,
        }
        quote_data.update(quote_data_overrides)
        if signature is None:
            quote = HashflowQuote.from_quote_data(quote_data)
            signature = "0x" + sign_maker(maker_key, quote.as_tuple(), chain["chainId"]).hex()
        return {
            "status": "success",
            "rfqId": RFQ_ID,
            "internalRfqIds": [RFQ_ID],
            "quotes": [
                {
                    "quoteData": quote_data,
                    "signature": signature,
                    "xChainFeeEstimate": None,
                    "targetContract": hashflow_router.address,
                    "calldata": None,
                    "value": None,
                    "hftTradingRewards": None,
                }
            ],
        }

    return _rfq


def keeper_publisher(harness, hashflow_adapter, keeper_account, network_config, http, **settings):
    """A publisher in keeper mode with a sending client."""
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    registry = AdapterRegistry(client, auction.registry())
    adapter = next(a for a in registry.active_adapters() if a.adapter == hashflow_adapter.address)
    config = network_config(
        mode="keeper", keeper_key_env="KEY", hashflow=HashflowSettings(**settings)
    )
    return HashflowPublisher(client, adapter, config, http=http), auction


def test_quote_is_checked_signed_relayed_and_the_proceeds_pushed(
    harness,
    network_config,
    http,
    adapter,
    hashflow_adapter,
    hashflow_router,
    hashflow_pool,
    sell_token,
    want,
    proceeds_receiver,
    keeper_account,
    lot,
    pool_liquidity,
    credential,
    rfq_for,
):
    publisher, auction = keeper_publisher(
        harness, hashflow_adapter, keeper_account, network_config, http
    )
    assert publisher.router.address == hashflow_router.address
    view = auction.lot_view(sell_token.address)
    http.on("POST", RFQ_PATH, rfq_for(view))
    assert sell_token.allowance(harness.address, hashflow_router.address) == 0

    result = publisher.publish(auction, view)

    assert result is not None
    # The RFQ body from the taker API v3 docs, the credential as Authorization.
    (call,) = http.requests("POST", RFQ_PATH)
    assert call["url"] == "https://api.hashflow.com/taker/v3/rfq"
    assert call["headers"]["Authorization"] == "hashflow-credential"
    chain = {"chainType": "evm", "chainId": 1}
    assert call["json"] == {
        "source": "api",
        "baseChain": chain,
        "quoteChain": chain,
        "rfqs": [
            {
                "baseToken": sell_token.address,
                "quoteToken": want.address,
                "baseTokenAmount": str(LOT_AMOUNT),
                "trader": harness.address,
                "rfqType": 1,
            }
        ],
    }
    # Signed as adapter ++ abi_encode(quote), valid on-chain for the router's digest.
    quote = HashflowQuote.from_quote_data(result.details["quote"] | {"txid": result.uid})
    taker_signature = bytes.fromhex(result.details["takerSignature"][2:])
    assert taker_signature == bytes(hashflow_adapter.signature_for(quote.as_tuple()))
    assert quote.digest(publisher.domain_separator) == quote_digest_reference(
        quote.as_tuple(), publisher.domain_separator
    )
    # Relayed through the router mock: the allowance synced first, the txid
    # burnt, the pool holds the lot, the proceeds pushed on to the receiver.
    # (the mock ERC-20 spends the synced max allowance)
    assert sell_token.allowance(harness.address, hashflow_router.address) == 2**256 - 1 - LOT_AMOUNT
    assert hashflow_router.usedTxids(quote.txid)
    assert sell_token.balanceOf(hashflow_pool.address) == LOT_AMOUNT
    assert harness.available(sell_token.address) == 0
    assert want.balanceOf(harness.address) == 0
    assert want.balanceOf(proceeds_receiver) == view.amount_needed + WAD
    assert result.details["relayed"] and result.details["pushed"] == view.amount_needed + WAD
    assert result.uid == "0x" + quote.txid.hex() and result.details["rfqId"] == RFQ_ID
    assert result.valid_to == timestamp() + 60
    assert result.sell_amount == LOT_AMOUNT and result.buy_amount == view.amount_needed + WAD
    assert [to for to, _ in publisher.client.sent] == [
        harness.address,
        hashflow_router.address,
        harness.address,
    ]


def test_offline_mode_validates_and_logs_the_calldata(
    client,
    auction,
    harness,
    network_config,
    http,
    adapter,
    hashflow_router,
    sell_token,
    want,
    lot,
    credential,
    rfq_for,
    caplog,
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    http.on("POST", RFQ_PATH, rfq_for(view))
    publisher = HashflowPublisher(client, adapter, network_config(), http=http)

    result = publisher.publish(auction, view)

    assert result is not None and client.sent == []
    assert "not relayed (offline mode" in caplog.text and "tradeRFQM" in caplog.text
    # The pair is valid on-chain for the router's digest (the lot is untouched).
    quote = HashflowQuote.from_quote_data(result.details["quote"] | {"txid": result.uid})
    taker_signature = bytes.fromhex(result.details["takerSignature"][2:])
    digest = quote_digest_reference(quote.as_tuple(), publisher.domain_separator)
    assert harness.isValidSignature(digest, taker_signature) == ERC1271_MAGIC_VALUE
    assert result.details["router"] == hashflow_router.address
    assert "relayed" not in result.details
    assert not hashflow_router.usedTxids(bytes.fromhex(result.uid[2:]))
    # The calldata logged is what any relayer sends: it settles as is.
    relayer = boa.env.generate_address("relayer")
    harness.sync_executor_approvals(hashflow_router.address, [sell_token.address])
    want._mint_for_testing(result.details["quote"]["pool"], view.amount_needed + WAD)
    boa.env.raw_call(
        hashflow_router.address,
        sender=relayer,
        data=bytes.fromhex(result.details["calldata"][2:]),
    )
    assert want.balanceOf(harness.address) == view.amount_needed + WAD
    # Offline, a want balance on the burner is reported, never sent.
    assert publisher.push_target(auction, view, dry_run=False) == view.amount_needed + WAD
    assert "run push_target()" in caplog.text and client.sent == []


def test_quote_below_the_curve_is_skipped(
    client, auction, network_config, http, adapter, sell_token, lot, credential, rfq_for, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    http.on("POST", RFQ_PATH, rfq_for(view, quote_amount=view.amount_needed - 1))
    publisher = HashflowPublisher(client, adapter, network_config(), http=http)
    assert publisher.publish(auction, view) is None
    assert "below the curve quote" in caplog.text
    # Exactly the curve is fine: check_order's bound is inclusive.
    http.handlers.clear()
    http.on("POST", RFQ_PATH, rfq_for(view, quote_amount=view.amount_needed))
    assert publisher.publish(auction, view) is not None


def test_quotes_for_the_wrong_lot_are_skipped(
    client,
    auction,
    network_config,
    http,
    adapter,
    sell_token,
    other_token,
    lot,
    credential,
    rfq_for,
    caplog,
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = HashflowPublisher(client, adapter, network_config(), http=http)
    for overrides, reason in (
        ({"trader": "0x" + "11" * 20}, "is not the auction"),
        ({"baseToken": other_token.address}, "are not the lot pair"),
        ({"quoteToken": other_token.address}, "are not the lot pair"),
        ({"base_amount": LOT_AMOUNT + 1}, "is not the requested"),
        ({"base_amount": LOT_AMOUNT // 2}, "is not the requested"),
        ({"expiry": view.end + 1}, "past the lot end"),
        ({"expiry": timestamp() - 1}, "already expired"),
        ({"rfqType": 0}, "is not RFQ-m"),
        ({"baseChain": {"chainType": "evm", "chainId": 137}}, "is not this chain"),
        ({"pool": "0x" + "00" * 20}, "no pool"),
    ):
        http.handlers.clear()
        http.on("POST", RFQ_PATH, rfq_for(view, **overrides))
        assert publisher.publish(auction, view) is None, reason
        assert reason in caplog.text, reason
    assert client.sent == []


def test_maker_signature_must_recover_to_the_pools_enabled_signer(
    client,
    auction,
    network_config,
    http,
    adapter,
    hashflow_pool,
    sell_token,
    lot,
    credential,
    rfq_for,
    caplog,
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = HashflowPublisher(client, adapter, network_config(), http=http)
    http.on("POST", RFQ_PATH, rfq_for(view, maker_key=keys.PrivateKey(keccak(OTHER_KEY_SEED))))
    assert publisher.publish(auction, view) is None
    assert "maker signature recovers to" in caplog.text

    http.handlers.clear()
    http.on("POST", RFQ_PATH, rfq_for(view, signature="0x" + "ab" * 64))
    assert publisher.publish(auction, view) is None
    assert "malformed maker signature" in caplog.text

    # An unauthorized pool (not created by the factory) has no signer to read.
    http.handlers.clear()
    http.on("POST", RFQ_PATH, rfq_for(view, pool=boa.env.generate_address("foreign_pool")))
    assert publisher.publish(auction, view) is None
    assert "signerConfiguration unreadable" in caplog.text

    http.handlers.clear()
    http.on("POST", RFQ_PATH, rfq_for(view))
    hashflow_pool.killswitchOperations(False)
    assert publisher.publish(auction, view) is None
    assert "is disabled" in caplog.text
    hashflow_pool.killswitchOperations(True)
    assert publisher.publish(auction, view) is not None


def test_proceeds_on_the_burner_are_pushed_every_poll_in_keeper_mode(
    harness,
    network_config,
    http,
    adapter,
    hashflow_adapter,
    sell_token,
    want,
    proceeds_receiver,
    keeper_account,
    lot,
    credential,
):
    # A fill relayed by someone else left target on the burner; the quote of
    # this poll fails, the push still happens first.
    want._mint_for_testing(harness.address, 7 * WAD)
    http.on("POST", RFQ_PATH, {"status": "fail", "error": "No quotes", "rfqId": RFQ_ID})
    publisher, auction = keeper_publisher(
        harness, hashflow_adapter, keeper_account, network_config, http
    )
    view = auction.lot_view(sell_token.address)
    assert publisher.publish(auction, view) is None
    assert want.balanceOf(harness.address) == 0
    assert want.balanceOf(proceeds_receiver) == 7 * WAD
    assert [to for to, _ in publisher.client.sent] == [harness.address]
    # Nothing held: nothing sent.
    assert publisher.publish(auction, view) is None
    assert len(publisher.client.sent) == 1
    # Dry run: reported, not sent.
    want._mint_for_testing(harness.address, WAD)
    assert publisher.publish(auction, view, dry_run=True) is None
    assert want.balanceOf(harness.address) == WAD and len(publisher.client.sent) == 1


def test_without_a_credential_the_template_is_logged(
    client, auction, harness, network_config, http, adapter, sell_token, lot, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("HASHFLOW_API_KEY", raising=False)
    view = auction.lot_view(sell_token.address)
    publisher = HashflowPublisher(client, adapter, network_config(), http=http)
    assert not publisher.feed_ready
    assert publisher.publish(auction, view) is None
    assert http.calls == []
    assert "quote request not sent" in caplog.text
    assert f"'trader': '{harness.address}'" in caplog.text
    assert f"'baseTokenAmount': '{LOT_AMOUNT}'" in caplog.text
    # post: false with a credential is the same.
    monkeypatch.setenv("HASHFLOW_API_KEY", "k")
    quiet = HashflowPublisher(
        client, adapter, network_config(hashflow=HashflowSettings(post=False)), http=http
    )
    assert quiet.publish(auction, view) is None and http.calls == []


def test_chunk_bounds_the_requested_amount(
    client, auction, harness, network_config, http, adapter, sell_token, lot, credential, rfq_for
):
    view = auction.lot_view(sell_token.address)
    chunk = LOT_AMOUNT // 5
    config = network_config(hashflow=HashflowSettings(chunk=chunk))
    publisher = HashflowPublisher(client, adapter, config, http=http)
    floor = harness.getAmountNeeded(sell_token.address, chunk)
    http.on(
        "POST", RFQ_PATH, lambda params, body: rfq_for(view, base_amount=chunk, quote_amount=floor)
    )
    result = publisher.publish(auction, view)
    assert result is not None
    (call,) = http.requests("POST", RFQ_PATH)
    assert call["json"]["rfqs"][0]["baseTokenAmount"] == str(chunk)
    assert result.sell_amount == chunk and result.buy_amount == floor
    # A chunk above the lot asks for everything available.
    wide = HashflowPublisher(
        client, adapter, network_config(hashflow=HashflowSettings(chunk=10 * LOT_AMOUNT)), http=http
    )
    assert wide.sell_amount(view) == LOT_AMOUNT


def test_api_failures_are_logged_not_raised(
    client, auction, network_config, http, adapter, sell_token, lot, credential, rfq_for, caplog
):
    caplog.set_level(logging.INFO)
    view = auction.lot_view(sell_token.address)
    publisher = HashflowPublisher(client, adapter, network_config(), http=http)
    http.on("POST", RFQ_PATH, {"status": "fail", "error": "No market makers", "rfqId": RFQ_ID})
    assert publisher.publish(auction, view) is None
    assert "No market makers" in caplog.text

    http.handlers.clear()

    def unauthorized(params, json):
        raise ApiError(401, {"code": 72, "message": "Unauthorized access"}, RFQ_PATH)

    http.on("POST", RFQ_PATH, unauthorized)
    assert publisher.publish(auction, view) is None
    assert "Unauthorized access" in caplog.text

    http.handlers.clear()
    http.on("POST", RFQ_PATH, {"status": "success", "rfqId": RFQ_ID, "quotes": []})
    assert publisher.publish(auction, view) is None

    http.handlers.clear()
    answer = rfq_for(view)
    answer["quotes"][0]["quoteData"]["txid"] = "0x1234"
    http.on("POST", RFQ_PATH, answer)
    assert publisher.publish(auction, view) is None
    assert "malformed quoteData" in caplog.text


def test_relay_revert_is_logged_and_nothing_recorded(
    harness,
    network_config,
    http,
    adapter,
    hashflow_adapter,
    hashflow_router,
    sell_token,
    keeper_account,
    lot,
    credential,
    rfq_for,
    caplog,
):
    # The pool has no liquidity: the router's call into it reverts, the
    # keeper logs the revert and leaves the quote unrecorded (and the txid unburnt).
    caplog.set_level(logging.INFO)
    publisher, auction = keeper_publisher(
        harness, hashflow_adapter, keeper_account, network_config, http
    )
    view = auction.lot_view(sell_token.address)
    http.on("POST", RFQ_PATH, rfq_for(view))
    assert publisher.publish(auction, view) is None
    assert "tradeRFQM for txid" in caplog.text and "reverted" in caplog.text
    assert harness.available(sell_token.address) == LOT_AMOUNT


def test_dry_run_requests_and_validates_but_relays_nothing(
    harness,
    network_config,
    http,
    adapter,
    hashflow_adapter,
    hashflow_router,
    sell_token,
    keeper_account,
    lot,
    pool_liquidity,
    credential,
    rfq_for,
):
    publisher, auction = keeper_publisher(
        harness, hashflow_adapter, keeper_account, network_config, http
    )
    view = auction.lot_view(sell_token.address)
    http.on("POST", RFQ_PATH, rfq_for(view))
    result = publisher.publish(auction, view, dry_run=True)
    assert result is not None and publisher.client.sent == []
    assert len(http.requests("POST", RFQ_PATH)) == 1
    assert harness.available(sell_token.address) == LOT_AMOUNT


# Config section


def test_config_section_defaults_and_parse(monkeypatch):
    from keepers.auction_keeper.config import ConfigError, parse_config

    auction = "0x" + "11" * 20
    hashflow = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction]}}}).network("x").hashflow
    )
    assert hashflow.enabled and hashflow.post and hashflow.chunk is None
    assert hashflow.api_key_env == "HASHFLOW_API_KEY" and hashflow.source == "api"
    assert hashflow.api_url == "https://api.hashflow.com"
    monkeypatch.setenv("HASHFLOW_API_KEY", "k4")
    assert hashflow.api_key == "k4"
    section = {"source": "curve", "chunk": 10**21, "post": False}
    hashflow = (
        parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], "hashflow": section}}})
        .network("x")
        .hashflow
    )
    assert hashflow.source == "curve" and hashflow.chunk == 10**21 and not hashflow.post
    # A nullable integer may be given as null.
    hashflow = (
        parse_config(
            {"networks": {"x": {"rpc": "r", "auctions": [auction], "hashflow": {"chunk": None}}}}
        )
        .network("x")
        .hashflow
    )
    assert hashflow.chunk is None
    for bad in ({"chunk": 0}, {"source": 1}, {"nope": True}, "polygon"):
        with pytest.raises(ConfigError):
            parse_config({"networks": {"x": {"rpc": "r", "auctions": [auction], "hashflow": bad}}})


# Keeper cycle over the Hashflow rail (keeper mode: the keeper relays the fill)


@pytest.fixture
def hashflow_rail(register, hashflow_adapter, hashflow_router, credential, pool_liquidity):
    register(hashflow_adapter, hashflow_router.address)
    return hashflow_adapter.address


@pytest.fixture
def hashflow_feeds(http, harness, hashflow_pool, sell_token):
    """The maker quotes the curve for the requested amount, five minutes out."""

    def hashflow_rfq(params, body):
        (rfq,) = body["rfqs"]
        base_amount = int(rfq["baseTokenAmount"])
        quote_amount = harness.getAmountNeeded(sell_token.address, base_amount)
        expiry = timestamp() + 300
        txid = keccak(f"hashflow-{timestamp()}-{base_amount}".encode())
        quote = (
            txid,
            rfq["trader"],
            hashflow_pool.address,
            "0x" + "00" * 20,
            rfq["baseToken"],
            rfq["quoteToken"],
            base_amount,
            quote_amount,
            expiry,
        )
        quote_data = {
            "rfqType": 1,
            "baseChain": body["baseChain"],
            "quoteChain": body["quoteChain"],
            "baseToken": rfq["baseToken"],
            "baseTokenAmount": rfq["baseTokenAmount"],
            "quoteToken": rfq["quoteToken"],
            "quoteTokenAmount": str(quote_amount),
            "trader": rfq["trader"],
            "txid": "0x" + txid.hex(),
            "pool": hashflow_pool.address,
            "quoteExpiry": expiry,
            "externalAccount": None,
        }
        signature = "0x" + sign_maker(HASHFLOW_MAKER_KEY, quote, body["baseChain"]["chainId"]).hex()
        return {
            "status": "success",
            "rfqId": RFQ_ID,
            "quotes": [{"quoteData": quote_data, "signature": signature}],
        }

    http.on("POST", RFQ_PATH, hashflow_rfq)
    return http


def keeper_mode(sell_token, **overrides):
    return dict(
        tokens=TokenFilter(whitelist=frozenset([sell_token.address])),
        mode="keeper",
        keeper_key_env="KEY",
        **overrides,
    )


def test_keeper_cycle_relays_in_chunks_and_pushes_the_proceeds(
    rail_keeper,
    network_config,
    hashflow_rail,
    hashflow_feeds,
    hashflow_router,
    orderbook,
    sell_token,
    want,
    harness,
    proceeds_receiver,
    lot,
    state,
    cow_adapter,
):
    chunk = LOT_AMOUNT // 2
    config = network_config(
        **keeper_mode(
            sell_token, poll_interval=60, order_ttl=300, hashflow=HashflowSettings(chunk=chunk)
        )
    )
    keeper = rail_keeper(config)
    assert {p.version for p in keeper.publishers_for(keeper.auctions[0])} == {
        "CowAdapter",
        "HashflowAdapter",
    }

    # The first chunk is quoted, relayed from the keeper account and the
    # proceeds (paid to the burner) pushed to the FeeCollector in one cycle.
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted", hashflow_rail: "posted"}
    assert len(orderbook.posted) == 1 and len(hashflow_feeds.requests("POST", RFQ_PATH)) == 1
    record = state.order("boa", harness.address, hashflow_rail, sell_token.address)
    assert record is not None and record.sell_amount == chunk
    assert record.lot_start == harness.window(sell_token.address)[0]
    assert record.valid_to == timestamp() + 300  # the quote's expiry
    assert hashflow_router.usedTxids(bytes.fromhex(record.uid[2:]))
    assert harness.available(sell_token.address) == LOT_AMOUNT - chunk
    assert want.balanceOf(harness.address) == 0
    assert want.balanceOf(proceeds_receiver) == record.buy_amount
    assert sell_token.balanceOf(harness.address) == LOT_AMOUNT - chunk

    # Within the quote's validity the record holds; expired, the next chunk
    # is quoted and relayed at the lower curve price.
    assert set(by_adapter(keeper.cycle()).values()) == {"kept"}
    boa.env.time_travel(seconds=config.order_ttl)
    assert set(by_adapter(keeper.cycle()).values()) == {"posted"}
    second = state.order("boa", harness.address, hashflow_rail, sell_token.address)
    assert second is not None and second.uid != record.uid
    assert second.buy_amount < record.buy_amount
    assert harness.available(sell_token.address) == 0
    assert want.balanceOf(proceeds_receiver) == record.buy_amount + second.buy_amount
    # The lot is sold out: nothing more to publish.
    assert by_adapter(keeper.cycle()) == {None: "skipped"}


def test_offline_mode_validates_and_records_without_relaying(
    rail_keeper,
    network_config,
    hashflow_rail,
    hashflow_feeds,
    hashflow_router,
    sell_token,
    harness,
    lot,
    state,
    cow_adapter,
):
    config = network_config(tokens=TokenFilter(whitelist=frozenset([sell_token.address])))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        hashflow_rail: "posted",
    }
    record = state.order("boa", harness.address, hashflow_rail, sell_token.address)
    assert record is not None and not hashflow_router.usedTxids(bytes.fromhex(record.uid[2:]))
    assert harness.available(sell_token.address) == LOT_AMOUNT


def test_disabled_rail_is_skipped(
    rail_keeper, network_config, hashflow_rail, hashflow_feeds, sell_token, lot, cow_adapter
):
    config = network_config(**keeper_mode(sell_token, hashflow=HashflowSettings(enabled=False)))
    keeper = rail_keeper(config)
    assert [p.version for p in keeper.publishers_for(keeper.auctions[0])] == ["CowAdapter"]
    assert by_adapter(keeper.cycle()) == {cow_adapter.address: "posted"}
    assert hashflow_feeds.calls == []


def test_one_rail_failing_does_not_stop_the_others(
    rail_keeper, network_config, hashflow_rail, hashflow_feeds, sell_token, lot, cow_adapter
):
    def boom(params, json):
        raise RuntimeError("rfq exploded")

    hashflow_feeds.handlers.insert(0, ("POST", RFQ_PATH, boom))
    config = network_config(**keeper_mode(sell_token))
    assert by_adapter(rail_keeper(config).cycle()) == {
        cow_adapter.address: "posted",
        hashflow_rail: "failed",
    }


def test_dry_run_quotes_but_relays_nothing(
    rail_keeper,
    network_config,
    hashflow_rail,
    hashflow_feeds,
    orderbook,
    sell_token,
    harness,
    lot,
    state,
):
    config = network_config(**keeper_mode(sell_token))
    outcomes = rail_keeper(config, dry_run=True).cycle()
    assert set(by_adapter(outcomes).values()) == {"posted"}
    assert orderbook.posted == [] and state.orders() == {}
    assert [c["url"].split("/")[-1] for c in hashflow_feeds.calls] == ["rfq"]
    assert harness.available(sell_token.address) == LOT_AMOUNT
