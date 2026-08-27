"""CowAdapter: the standalone GPv2 settlement adapter behind the fallback route.

Validation runs end-to-end through the auction's signature router: the harness
forwards both historical CoW encodings verbatim, the adapter proves the
canonical digest and protocol constants, and every economic decision comes
from the auction's shared check_order view — reverted verbatim in the
watchtower-canonical OrderNotValid ABI.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT256 = 2**256 - 1
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
DECAY_FACTOR_RAY = 992031276831159793484252056
LOT_AMOUNT = 250 * WAD

APP_DATA = keccak(b"CURVE_DUTCH_AUCTION_TEST_APP_DATA")
ORDER_VALIDITY = 120
DOMAIN_SEPARATOR = keccak(b"GPV2_TEST_DOMAIN")
FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")

SELL_KIND = keccak(text="sell")
BUY_KIND = keccak(text="buy")
ERC20_BALANCE = keccak(text="erc20")
EXTERNAL_BALANCE = keccak(text="external")

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")

ORDER_TYPE = (
    "(address,address,address,uint256,uint256,uint32,bytes32,uint256,"
    "bytes32,bool,bytes32,bytes32)"
)
PAYLOAD_TYPE = "(bytes32[],(address,bytes32,bytes),bytes)"

ORDER_SELL_TOKEN = 0
ORDER_BUY_TOKEN = 1
ORDER_RECEIVER = 2
ORDER_SELL_AMOUNT = 3
ORDER_BUY_AMOUNT = 4
ORDER_VALID_TO = 5
ORDER_APP_DATA = 6
ORDER_FEE_AMOUNT = 7
ORDER_KIND = 8
ORDER_PARTIALLY_FILLABLE = 9
ORDER_SELL_BALANCE = 10
ORDER_BUY_BALANCE = 11


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="module")
def emergency_owner():
    return boa.env.generate_address("emergency_owner")


@pytest.fixture(scope="module")
def relayer():
    return boa.env.generate_address("relayer")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def erc20_deployer():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def want(erc20_deployer):
    return erc20_deployer.deploy("Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def sell_token(erc20_deployer):
    return erc20_deployer.deploy("Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def role_source(owner, emergency_owner):
    return boa.load(
        "contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner
    )


@pytest.fixture(scope="module")
def settlement_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/SettlementMock.vy")


@pytest.fixture(scope="module")
def settlement(settlement_deployer, relayer):
    return settlement_deployer.deploy(DOMAIN_SEPARATOR, relayer)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/cow/CowAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load(
        "contracts/burners/auction/adapters/AdapterRegistry.vy", role_source.address
    )


@pytest.fixture
def adapter(adapter_deployer, settlement):
    return adapter_deployer.deploy(settlement, APP_DATA, ORDER_VALIDITY)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, relayer):
    harness = boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry.address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
    )
    with boa.env.prank(owner):
        registry.set_adapter(adapter, relayer)
        registry.activate_adapter(adapter)
        harness.enable_adapter(adapter)
        harness.set_fallback_adapter(adapter)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _order_digest(order: tuple, domain_separator: bytes) -> bytes:
    order_type_hash = keccak(
        text=(
            "Order(address sellToken,address buyToken,address receiver,uint256 sellAmount,"
            "uint256 buyAmount,uint32 validTo,bytes32 appData,uint256 feeAmount,string kind,"
            "bool partiallyFillable,string sellTokenBalance,string buyTokenBalance)"
        )
    )
    struct_hash = keccak(
        encode(
            [
                "bytes32",
                "address",
                "address",
                "address",
                "uint256",
                "uint256",
                "uint32",
                "bytes32",
                "uint256",
                "bytes32",
                "bool",
                "bytes32",
                "bytes32",
            ],
            [order_type_hash, *order],
        )
    )
    return keccak(b"\x19\x01" + bytes(domain_separator) + struct_hash)


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, lot):
    def _make(**overrides) -> tuple:
        amount = overrides.pop("sell_amount", LOT_AMOUNT)
        buy_amount = overrides.pop("buy_amount", None)
        if buy_amount is None:
            buy_amount = harness.quote(sell_token.address, min(amount, LOT_AMOUNT))
        order = {
            "sell_token": sell_token.address,
            "buy_token": want.address,
            "receiver": proceeds_receiver,
            "sell_amount": amount,
            "buy_amount": buy_amount,
            "valid_to": harness.frame_end(),
            "app_data": APP_DATA,
            "fee_amount": 0,
            "kind": SELL_KIND,
            "partially_fillable": True,
            "sell_balance": ERC20_BALANCE,
            "buy_balance": ERC20_BALANCE,
        }
        order.update(overrides)
        return tuple(order.values())

    return _make


def _bare(order: tuple) -> bytes:
    return encode([ORDER_TYPE], [order])


def _wrapper(order: tuple, payload: tuple | None = None) -> bytes:
    payload = payload or ([], (ZERO_ADDRESS, bytes(32), b""), b"")
    return encode([ORDER_TYPE, PAYLOAD_TYPE], [order, payload])


def _validate(harness, order: tuple, signature: bytes | None = None, domain=DOMAIN_SEPARATOR):
    signature = _bare(order) if signature is None else signature
    return harness.isValidSignature(_order_digest(order, domain), signature)


# Deployment pinning


def test_deploy_pins_settlement_domain_and_relayer(adapter, settlement, relayer):
    assert adapter.settlement() == settlement.address
    assert bytes(adapter.domain_separator()) == DOMAIN_SEPARATOR
    assert adapter.vault_relayer() == relayer
    assert bytes(adapter.app_data()) == APP_DATA
    assert adapter.order_validity() == ORDER_VALIDITY
    assert adapter.ADAPTER_VERSION() == "CowAdapter"


def test_deploy_rejects_zero_settlement(adapter_deployer):
    with boa.reverts(custom_err("BadSettlement()")):
        adapter_deployer.deploy(ZERO_ADDRESS, APP_DATA, ORDER_VALIDITY)


def test_deploy_rejects_zero_order_validity(adapter_deployer, settlement):
    with boa.reverts(custom_err("BadCowValidity()")):
        adapter_deployer.deploy(settlement, APP_DATA, 0)


def test_deploy_rejects_missing_domain_separator(settlement_deployer, adapter_deployer, relayer):
    empty_domain = settlement_deployer.deploy(bytes(32), relayer)
    with boa.reverts(custom_err("BadDomainSeparator()")):
        adapter_deployer.deploy(empty_domain, APP_DATA, ORDER_VALIDITY)


def test_deploy_rejects_missing_vault_relayer(settlement_deployer, adapter_deployer):
    no_relayer = settlement_deployer.deploy(DOMAIN_SEPARATOR, ZERO_ADDRESS)
    with boa.reverts(custom_err("BadVaultRelayer()")):
        adapter_deployer.deploy(no_relayer, APP_DATA, ORDER_VALIDITY)


# Transport encodings


def test_bare_order_signature_valid(harness, make_order):
    assert _validate(harness, make_order()) == ERC1271_MAGIC_VALUE


def test_partial_fill_order_valid(harness, make_order):
    order = make_order(sell_amount=LOT_AMOUNT // 3)
    assert _validate(harness, order) == ERC1271_MAGIC_VALUE


def test_wrapper_signature_valid(harness, make_order):
    order = make_order()
    assert _validate(harness, order, _wrapper(order)) == ERC1271_MAGIC_VALUE


def test_wrapper_is_transport_not_authority(harness, make_order):
    # Junk registration payloads change nothing: only the inner order decides.
    order = make_order()
    junk = (
        [keccak(b"proof")],
        (boa.env.generate_address("handler"), keccak(b"salt"), b"\x01" * 52),
        b"",
    )
    assert _validate(harness, order, _wrapper(order, junk)) == ERC1271_MAGIC_VALUE


def test_non_canonical_padded_wrapper_rejected(harness, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, order, _wrapper(order) + b"\x00")


def test_malformed_signature_bytes_revert(harness, make_order):
    order = make_order()
    with boa.reverts():
        harness.isValidSignature(_order_digest(order, DOMAIN_SEPARATOR), b"\x01" * 52)


def test_zero_filled_order_fails_checks_not_decode(harness, lot):
    zero_order = (ZERO_ADDRESS, ZERO_ADDRESS, ZERO_ADDRESS, 0, 0, 0, bytes(32), 0,
                  bytes(32), False, bytes(32), bytes(32))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAppData")):
        _validate(harness, zero_order)


# Protocol constants


def test_hash_mismatch_invalid(harness, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _bare(order))


def test_foreign_domain_separator_invalid(harness, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, order, domain=FOREIGN_DOMAIN)


def test_wrong_app_data_invalid(harness, make_order):
    order = make_order(app_data=keccak(b"other app data"))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAppData")):
        _validate(harness, order)


@pytest.mark.parametrize(
    "overrides",
    [
        {"fee_amount": 1},
        {"kind": BUY_KIND},
        {"partially_fillable": False},
    ],
)
def test_order_flag_violations_invalid(harness, make_order, overrides):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderFlags")):
        _validate(harness, make_order(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [{"sell_balance": EXTERNAL_BALANCE}, {"buy_balance": EXTERNAL_BALANCE}],
)
def test_balance_mode_violations_invalid(harness, make_order, overrides):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBalanceMode")):
        _validate(harness, make_order(**overrides))


# Economic checks bubble from check_order


def test_wrong_buy_token_invalid(harness, make_order, sell_token):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadToken")):
        _validate(harness, make_order(buy_token=sell_token.address))


def test_wrong_receiver_invalid(harness, make_order):
    stranger = boa.env.generate_address("stranger")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadReceiver")):
        _validate(harness, make_order(receiver=stranger))


def test_unstaged_token_not_allowed(harness, make_order, erc20_deployer, want):
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    order = make_order(sell_token=fresh.address, buy_amount=1)
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        _validate(harness, order)


def test_unsellable_token_not_allowed(harness, make_order, sell_token):
    order = make_order()
    harness.set_sellable(sell_token.address, False)
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        _validate(harness, order)


def test_expired_window_not_allowed(harness, make_order):
    order = make_order()
    boa.env.time_travel(seconds=harness.frame_end() - boa.env.evm.vm.state.timestamp)
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        _validate(harness, order)


def test_drained_lot_zero_balance(harness, make_order, sell_token):
    order = make_order()
    with boa.env.prank(harness.address):
        sell_token.transfer(boa.env.generate_address("sink"), LOT_AMOUNT)
    with boa.reverts(custom_err("OrderNotValid(string)", "ZeroBalance")):
        _validate(harness, order)


@pytest.mark.parametrize("sell_amount", [0, LOT_AMOUNT + 1])
def test_sell_amount_bounds_invalid(harness, make_order, sell_amount):
    order = make_order(sell_amount=sell_amount, buy_amount=1)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSellAmount")):
        _validate(harness, order)


def test_valid_to_bounds_invalid(harness, make_order):
    now = boa.env.evm.vm.state.timestamp
    with boa.reverts(custom_err("OrderNotValid(string)", "BadValidTo")):
        _validate(harness, make_order(valid_to=now - 1))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadValidTo")):
        _validate(harness, make_order(valid_to=harness.frame_end() + 1))


def test_underpriced_order_invalid(harness, make_order, sell_token):
    quote = harness.quote(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBuyAmount")):
        _validate(harness, make_order(buy_amount=quote - 1))
    # Overpriced orders are always fine for the receiver.
    assert _validate(harness, make_order(buy_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_quote_is_live_against_lot_snapshot(harness, make_order, sell_token):
    # A donation above the snapshot never lowers the required unit price:
    # sell amounts stay bounded by initial_amount, quotes by the live curve.
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    order = make_order()
    assert _validate(harness, order) == ERC1271_MAGIC_VALUE
    too_big = make_order(sell_amount=LOT_AMOUNT + 1, buy_amount=MAX_UINT256)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSellAmount")):
        _validate(harness, too_big)
