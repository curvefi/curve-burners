"""CowPublisher against the real CowAdapter and the auction's signature router."""

import boa
import pytest
from eth_utils import keccak

from keepers.auction_keeper.chain.boa_client import BoaClient
from keepers.auction_keeper.chain.contracts import (
    ERC1271_MAGIC_VALUE,
    AdapterRegistry,
    Auction,
    Erc20,
)
from keepers.auction_keeper.config import CowSettings
from keepers.auction_keeper.rails.cow import CowPublisher, GPv2Order
from tests.keeper.conftest import (
    APP_DATA,
    DOMAIN_SEPARATOR,
    LOT_AMOUNT,
    FakeOrderbook,
    timestamp,
)


@pytest.fixture
def client():
    return BoaClient()


@pytest.fixture
def auction(client, harness):
    return Auction(client, harness.address)


def make_publisher(client, auction, config, orderbook):
    registry = AdapterRegistry(client, auction.registry())
    (adapter,) = registry.active_adapters()
    return CowPublisher(client, adapter, config, orderbook=orderbook)


def test_reads_adapter_wiring(client, auction, network_config, orderbook, cow_adapter, relayer):
    publisher = make_publisher(client, auction, network_config(), orderbook)
    assert publisher.version == "CowAdapter"
    assert publisher.address == cow_adapter.address
    assert publisher.app_data == APP_DATA
    assert publisher.domain_separator == DOMAIN_SEPARATOR
    assert publisher.vault_relayer == relayer


def test_order_matches_adapter_template_and_validates(
    client, auction, harness, network_config, orderbook, cow_adapter, sell_token, lot
):
    publisher = make_publisher(client, auction, network_config(order_ttl=10**6), orderbook)
    view = auction.lot_view(sell_token.address)
    order = publisher.build_order(view, publisher.valid_to(view))

    # Same fields as the on-chain helper for the full window.
    expected, expected_signature = cow_adapter.order_for(harness.address, sell_token.address)
    assert order.as_tuple() == tuple(expected)
    assert order.signature(cow_adapter.address) == bytes(expected_signature)
    assert publisher.validate(auction, order) is None
    assert (
        harness.isValidSignature(
            order.digest(DOMAIN_SEPARATOR), order.signature(cow_adapter.address)
        )
        == ERC1271_MAGIC_VALUE
    )


def test_ttl_shortens_valid_to_but_never_past_the_window(
    client, auction, network_config, orderbook, sell_token, lot
):
    view = auction.lot_view(sell_token.address)
    short = make_publisher(client, auction, network_config(order_ttl=300), orderbook)
    assert short.valid_to(view) == view.timestamp + 300
    long = make_publisher(client, auction, network_config(order_ttl=10**9), orderbook)
    assert long.valid_to(view) == view.end
    # A shortened validity still passes the auction's economic check.
    assert short.validate(auction, short.build_order(view, short.valid_to(view))) is None


def test_underpriced_order_is_rejected_with_reason(
    client, auction, network_config, orderbook, sell_token, lot
):
    publisher = make_publisher(client, auction, network_config(), orderbook)
    view = auction.lot_view(sell_token.address)
    order = publisher.build_order(view, publisher.valid_to(view))
    cheap = GPv2Order(**{**order.__dict__, "buy_amount": order.buy_amount - 1})
    assert publisher.validate(auction, cheap) == "BadBuyAmount()"
    wrong_app_data = GPv2Order(**{**order.__dict__, "app_data": keccak(b"other")})
    assert publisher.validate(auction, wrong_app_data) == "OrderNotValid('BadAppData')"


def test_publish_posts_eip1271_order_and_syncs_allowance(
    harness,
    network_config,
    orderbook,
    cow_adapter,
    sell_token,
    want,
    proceeds_receiver,
    relayer,
    lot,
    keeper_account,
):
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    config = network_config(order_ttl=300, mode="keeper", keeper_key_env="KEY")
    publisher = make_publisher(client, auction, config, orderbook)
    view = auction.lot_view(sell_token.address)
    assert sell_token.allowance(harness.address, relayer) == 0

    result = publisher.publish(auction, view)

    assert result is not None
    assert sell_token.allowance(harness.address, relayer) == 2**256 - 1
    (body,) = orderbook.posted
    assert body["from"] == harness.address
    assert body["signingScheme"] == "eip1271"
    assert body["sellToken"] == sell_token.address
    assert body["buyToken"] == want.address
    assert body["receiver"] == proceeds_receiver
    assert body["sellAmount"] == str(LOT_AMOUNT) == str(result.sell_amount)
    assert int(body["buyAmount"]) == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert body["validTo"] == timestamp() + 300 == result.valid_to
    assert body["appData"] == "0x" + APP_DATA.hex()
    assert body["kind"] == "sell" and body["partiallyFillable"] is True
    assert body["feeAmount"] == "0"
    assert body["signature"] == "0x" + cow_adapter.address[2:].lower() + body["signature"][42:]
    # The posted signature validates on-chain for the order's digest.
    signature = bytes.fromhex(body["signature"][2:])
    digest = bytes.fromhex(result.uid[2:])[:32]
    assert harness.isValidSignature(digest, signature) == ERC1271_MAGIC_VALUE
    assert result.uid.endswith(
        harness.address[2:].lower() + result.valid_to.to_bytes(4, "big").hex()
    )


def test_offline_mode_posts_without_allowance_and_names_the_sync(
    client, auction, harness, network_config, orderbook, sell_token, relayer, lot, caplog
):
    publisher = make_publisher(client, auction, network_config(), orderbook)
    view = auction.lot_view(sell_token.address)
    assert publisher.publish(auction, view) is not None
    assert len(orderbook.posted) == 1
    assert client.sent == []
    assert sell_token.allowance(harness.address, relayer) == 0
    assert "offline mode" in caplog.text and "sync_executor_approvals" in caplog.text


def test_keeper_mode_never_posts_without_a_sending_client(
    client, auction, network_config, orderbook, sell_token, lot, caplog
):
    config = network_config(mode="keeper", keeper_key_env="KEY")
    publisher = make_publisher(client, auction, config, orderbook)
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is None
    assert orderbook.posted == []
    assert "keeper mode without a sending client" in caplog.text


def test_publish_with_existing_allowance_needs_no_key(
    client, auction, harness, network_config, orderbook, sell_token, relayer, lot, keeper_account
):
    with boa.env.prank(keeper_account):
        harness.sync_executor_approvals(relayer, [sell_token.address])
    publisher = make_publisher(client, auction, network_config(), orderbook)
    assert publisher.publish(auction, auction.lot_view(sell_token.address)) is not None
    assert len(orderbook.posted) == 1


def test_dry_run_posts_nothing(client, auction, network_config, orderbook, sell_token, lot):
    publisher = make_publisher(client, auction, network_config(), orderbook)
    result = publisher.publish(auction, auction.lot_view(sell_token.address), dry_run=True)
    assert result is not None and orderbook.posted == []


def test_inactive_lot_is_not_published(client, auction, network_config, orderbook, other_token):
    publisher = make_publisher(client, auction, network_config(), orderbook)
    view = auction.lot_view(other_token.address)
    assert not view.active
    assert publisher.publish(auction, view) is None


def test_app_data_document_registered_once_and_checked(
    harness, network_config, orderbook, sell_token, relayer, lot, keeper_account
):
    client = BoaClient(sender=keeper_account)
    auction = Auction(client, harness.address)
    document = "CURVE_DUTCH_AUCTION_KEEPER_TEST_APP_DATA"  # keccak(document) == APP_DATA
    config = network_config(cow=CowSettings(app_data_document=document))
    publisher = make_publisher(client, auction, config, orderbook)
    view = auction.lot_view(sell_token.address)
    publisher.publish(auction, view)
    publisher.publish(auction, view)
    assert orderbook.app_data == {"0x" + APP_DATA.hex(): document}

    bad = make_publisher(
        client,
        auction,
        network_config(cow=CowSettings(app_data_document="other")),
        FakeOrderbook(DOMAIN_SEPARATOR),
    )
    with pytest.raises(ValueError, match="app_data_document"):
        bad.publish(auction, view)


def test_erc20_reader(client, sell_token, harness, lot):
    token = Erc20(client, sell_token.address)
    assert token.symbol() == "CRV"
    assert token.decimals() == 18
    assert token.balance_of(harness.address) == LOT_AMOUNT
