"""Standalone tests for the stateful cow_execution module.

Exercises contracts/burners/cow/execution.vy in isolation through
CowExecutionHarness: the configure/enable/disable lifecycle with settlement
discovery via SettlementMock, and ERC-1271 validation of bare Yearn-style
and ComposableCoW-wrapped GPv2 orders against the controllable hook state
(target, receiver, lot context, quote, signature gate). The stateless gpv2
helpers it builds on are covered in test_gpv2_module.py.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from .conftest import custom_err


WAD = 10**18
APP_DATA = keccak(b"cow execution app data")
DOMAIN_SEPARATOR = keccak(b"cow execution settlement domain")
ORDER_TYPE_HASH = bytes.fromhex("d5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489")
SELL_KIND = keccak(text="sell")
ERC20_BALANCE = keccak(text="erc20")
ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ZERO_BYTES32 = bytes(32)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

COW_ORDER_VALIDITY = 120
ENCODED_ORDER_LEN = 12 * 32

INITIAL_AMOUNT = 100 * WAD
AVAILABLE = 80 * WAD
QUOTE = 40 * WAD
SELL_AMOUNT = 50 * WAD
LOT_DURATION = 3600

# GPv2Order.Data tuple fields.
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

ORDER_FIELD_TYPES = [
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
]
ORDER_TUPLE_TYPE = (
    "(address,address,address,uint256,uint256,uint32,bytes32,uint256,bytes32,bool,bytes32,bytes32)"
)
PAYLOAD_TUPLE_TYPE = "(bytes32[],(address,bytes32,bytes),bytes)"


@dataclass(frozen=True)
class Lot:
    start: int
    end: int


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _event_name(log: Any) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


def order_digest(order: list, domain_separator: bytes = DOMAIN_SEPARATOR) -> bytes:
    """Independent EIP-712 digest model built from eth_abi/keccak primitives."""
    struct_hash = keccak(encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def encode_bare_signature(order: list) -> bytes:
    """Yearn-style self-published signature: abi_encode(GPv2Order)."""
    signature = encode(ORDER_FIELD_TYPES, order)
    assert len(signature) == ENCODED_ORDER_LEN
    return signature


def encode_wrapper_signature(order: list, handler: str) -> bytes:
    """Watchtower-published signature: abi_encode(GPv2Order, PayloadStruct)."""
    payload = ([], (handler, ZERO_BYTES32, b"\xee" * 52), b"")
    return encode([ORDER_TUPLE_TYPE, PAYLOAD_TUPLE_TYPE], [tuple(order), payload])


def assert_order_not_valid(harness: Any, order: list, reason: str) -> None:
    with boa.reverts(custom_err("OrderNotValid(string)", reason)):
        harness.isValidSignature(order_digest(order), encode_bare_signature(order))


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def vault_relayer():
    return boa.env.generate_address("vault_relayer")


@pytest.fixture(scope="module")
def sell_token():
    return boa.env.generate_address("sell_token")


@pytest.fixture(scope="module")
def want_token():
    return boa.env.generate_address("want_token")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def settlement_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/SettlementMock.vy")


@pytest.fixture(scope="module")
def settlement(settlement_deployer, vault_relayer):
    return settlement_deployer.deploy(DOMAIN_SEPARATOR, vault_relayer)


@pytest.fixture(scope="module")
def harness_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/CowExecutionHarness.vy")


@pytest.fixture(scope="module")
def harness(harness_deployer, want_token, proceeds_receiver):
    harness = harness_deployer.deploy(APP_DATA, COW_ORDER_VALIDITY)
    harness.set_cow_target(want_token)
    harness.set_cow_receiver(proceeds_receiver)
    return harness


@pytest.fixture
def lot(harness, settlement, sell_token):
    """Configure and enable the rail, then open a live lot for sell_token."""
    harness.configure_cow(settlement)
    harness.enable_cow()
    now = _timestamp()
    start, end = now - 10, now + LOT_DURATION
    harness.set_context(sell_token, True, AVAILABLE, INITIAL_AMOUNT, start, end)
    harness.set_quote(QUOTE)
    return Lot(start=start, end=end)


@pytest.fixture
def valid_order(lot, sell_token, want_token, proceeds_receiver):
    return [
        sell_token,
        want_token,
        proceeds_receiver,
        SELL_AMOUNT,
        QUOTE,
        _timestamp() + COW_ORDER_VALIDITY,
        APP_DATA,
        0,
        SELL_KIND,
        True,
        ERC20_BALANCE,
        ERC20_BALANCE,
    ]


# Deployment


def test_deploy_starts_unconfigured_and_disabled(harness):
    assert bytes(harness.app_data()) == APP_DATA
    assert harness.cow_order_validity() == COW_ORDER_VALIDITY
    assert not harness.cow_enabled()
    assert harness.settlement() == ZERO_ADDRESS
    assert harness.vault_relayer() == ZERO_ADDRESS
    assert bytes(harness.cow_domain_separator()) == ZERO_BYTES32


def test_deploy_rejects_zero_order_validity(harness_deployer):
    with boa.reverts(custom_err("BadCowValidity()")):
        harness_deployer.deploy(APP_DATA, 0)


# Configure/enable/disable lifecycle


def test_configure_discovers_settlement_state_and_emits(harness, settlement, vault_relayer):
    harness.configure_cow(settlement)
    configured = next(
        log for log in harness.get_logs() if _event_name(log) == "CowExecutionConfigured"
    )
    assert harness.settlement() == settlement.address
    assert harness.vault_relayer() == vault_relayer
    assert bytes(harness.cow_domain_separator()) == DOMAIN_SEPARATOR
    # Configuring alone never turns the rail on.
    assert not harness.cow_enabled()

    assert configured.settlement == settlement.address
    assert configured.vault_relayer == vault_relayer
    assert bytes(configured.domain_separator) == DOMAIN_SEPARATOR


def test_configure_rejects_zero_settlement(harness):
    with boa.reverts(custom_err("BadSettlement()")):
        harness.configure_cow(ZERO_ADDRESS)


def test_configure_rejects_missing_domain_separator(harness, settlement_deployer, vault_relayer):
    hollow_settlement = settlement_deployer.deploy(ZERO_BYTES32, vault_relayer)
    with boa.reverts(custom_err("BadDomainSeparator()")):
        harness.configure_cow(hollow_settlement)


def test_configure_rejects_missing_vault_relayer(harness, settlement_deployer):
    hollow_settlement = settlement_deployer.deploy(DOMAIN_SEPARATOR, ZERO_ADDRESS)
    with boa.reverts(custom_err("BadVaultRelayer()")):
        harness.configure_cow(hollow_settlement)


def test_reconfigure_while_disabled_replaces_settlement(harness, settlement, settlement_deployer):
    harness.configure_cow(settlement)

    new_relayer = boa.env.generate_address("new_relayer")
    new_domain = keccak(b"migrated settlement domain")
    new_settlement = settlement_deployer.deploy(new_domain, new_relayer)
    harness.configure_cow(new_settlement)

    # Relayer and domain separator are re-read together so they cannot drift.
    assert harness.settlement() == new_settlement.address
    assert harness.vault_relayer() == new_relayer
    assert bytes(harness.cow_domain_separator()) == new_domain
    harness.enable_cow()
    assert harness.cow_enabled()


def test_configure_blocked_while_enabled(harness, settlement):
    harness.configure_cow(settlement)
    harness.enable_cow()
    with boa.reverts(custom_err("CowAlreadyEnabled()")):
        harness.configure_cow(settlement)


def test_enable_requires_configuration(harness):
    with boa.reverts(custom_err("CowUnconfigured()")):
        harness.enable_cow()


def test_enable_disable_lifecycle(harness, settlement, vault_relayer):
    harness.configure_cow(settlement)
    harness.enable_cow()
    assert any(_event_name(log) == "CowExecutionEnabled" for log in harness.get_logs())
    assert harness.cow_enabled()
    with boa.reverts(custom_err("CowAlreadyEnabled()")):
        harness.enable_cow()

    harness.disable_cow()
    assert any(_event_name(log) == "CowExecutionDisabled" for log in harness.get_logs())
    assert not harness.cow_enabled()
    with boa.reverts(custom_err("CowNotEnabled()")):
        harness.disable_cow()

    # Disabling keeps the configuration, so re-enable needs no reconfigure.
    assert harness.settlement() == settlement.address
    assert harness.vault_relayer() == vault_relayer
    harness.enable_cow()
    assert harness.cow_enabled()


# ERC-1271 validation: accepted encodings


def test_bare_order_signature_valid(harness, valid_order):
    signature = encode_bare_signature(valid_order)
    assert harness.isValidSignature(order_digest(valid_order), signature) == ERC1271_MAGIC_VALUE


def test_full_lot_sell_amount_valid(harness, valid_order):
    # sellAmount is bounded by the lot's initial amount, not the remaining
    # available balance: partial fills already in flight must not invalidate
    # a full-size published order.
    order = deepcopy(valid_order)
    order[ORDER_SELL_AMOUNT] = INITIAL_AMOUNT
    assert (
        harness.isValidSignature(order_digest(order), encode_bare_signature(order))
        == ERC1271_MAGIC_VALUE
    )


def test_wrapper_signature_valid(harness, valid_order):
    handler = boa.env.generate_address("handler")
    signature = encode_wrapper_signature(valid_order, handler)
    assert len(signature) != ENCODED_ORDER_LEN
    assert harness.isValidSignature(order_digest(valid_order), signature) == ERC1271_MAGIC_VALUE


def test_wrapper_is_transport_not_authority(harness, valid_order):
    # A different handler/payload changes nothing: only the inner order's
    # economics decide, so a registration can never weaken settlement checks.
    attacker_handler = boa.env.generate_address("attacker_handler")
    signature = encode_wrapper_signature(valid_order, attacker_handler)
    assert harness.isValidSignature(order_digest(valid_order), signature) == ERC1271_MAGIC_VALUE

    underpriced = deepcopy(valid_order)
    underpriced[ORDER_BUY_AMOUNT] = QUOTE - 1
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBuyAmount")):
        harness.isValidSignature(
            order_digest(underpriced), encode_wrapper_signature(underpriced, attacker_handler)
        )


# ERC-1271 validation: rail gates


def test_configured_but_not_enabled_reverts_cow_disabled(harness, settlement, valid_order):
    signature = encode_bare_signature(valid_order)
    order_hash = order_digest(valid_order)
    harness.disable_cow()
    with boa.reverts(custom_err("CowDisabled()")):
        harness.isValidSignature(order_hash, signature)

    harness.enable_cow()
    assert harness.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE


def test_unconfigured_rail_reverts_cow_disabled(harness_deployer, valid_order):
    fresh = harness_deployer.deploy(APP_DATA, COW_ORDER_VALIDITY)
    with boa.reverts(custom_err("CowDisabled()")):
        fresh.isValidSignature(order_digest(valid_order), encode_bare_signature(valid_order))


def test_signature_allowed_hook_gates_validation(harness, valid_order):
    signature = encode_bare_signature(valid_order)
    order_hash = order_digest(valid_order)
    harness.set_signature_allowed(False)
    with boa.reverts(custom_err("OrderNotValid(string)", "Reentrancy")):
        harness.isValidSignature(order_hash, signature)

    harness.set_signature_allowed(True)
    assert harness.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE


# ERC-1271 validation: digest and identity checks


def test_hash_mismatch_invalid(harness, valid_order):
    signature = encode_bare_signature(valid_order)
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"unrelated hash"), signature)


def test_foreign_domain_separator_invalid(harness, valid_order):
    # A digest signed for another settlement domain never matches this rail.
    foreign_hash = order_digest(valid_order, keccak(b"foreign settlement domain"))
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(foreign_hash, encode_bare_signature(valid_order))


def test_wrong_app_data_invalid(harness, valid_order):
    order = deepcopy(valid_order)
    order[ORDER_APP_DATA] = keccak(b"someone else's app data")
    assert_order_not_valid(harness, order, "BadAppData")


def test_wrong_buy_token_invalid(harness, valid_order, sell_token):
    order = deepcopy(valid_order)
    order[ORDER_BUY_TOKEN] = sell_token
    assert_order_not_valid(harness, order, "BadToken")


def test_buy_token_follows_target_hook(harness, valid_order):
    # The target hook is consulted live: retargeting invalidates old orders.
    new_target = boa.env.generate_address("new_target")
    harness.set_cow_target(new_target)
    assert_order_not_valid(harness, valid_order, "BadToken")

    order = deepcopy(valid_order)
    order[ORDER_BUY_TOKEN] = new_target
    assert (
        harness.isValidSignature(order_digest(order), encode_bare_signature(order))
        == ERC1271_MAGIC_VALUE
    )


def test_wrong_receiver_invalid(harness, valid_order):
    order = deepcopy(valid_order)
    order[ORDER_RECEIVER] = boa.env.generate_address("attacker")
    assert_order_not_valid(harness, order, "BadReceiver")


@pytest.mark.parametrize(
    "index,value",
    [
        (ORDER_FEE_AMOUNT, 1),
        (ORDER_KIND, ERC20_BALANCE),  # buy-kind or garbage kind
        (ORDER_PARTIALLY_FILLABLE, False),
    ],
)
def test_order_flag_violations_invalid(harness, valid_order, index, value):
    order = deepcopy(valid_order)
    order[index] = value
    assert_order_not_valid(harness, order, "BadOrderFlags")


@pytest.mark.parametrize("index", [ORDER_SELL_BALANCE, ORDER_BUY_BALANCE])
def test_balance_mode_violations_invalid(harness, valid_order, index):
    order = deepcopy(valid_order)
    order[index] = SELL_KIND  # any non-erc20 balance mode
    assert_order_not_valid(harness, order, "BadBalanceMode")


# ERC-1271 validation: lot context checks


def test_inactive_lot_invalid(harness, valid_order, sell_token, lot):
    harness.set_context(sell_token, False, AVAILABLE, INITIAL_AMOUNT, lot.start, lot.end)
    assert_order_not_valid(harness, valid_order, "NotAllowed")


def test_unknown_sell_token_invalid(harness, valid_order):
    # No context was ever set for this token: the empty context is inactive.
    order = deepcopy(valid_order)
    order[ORDER_SELL_TOKEN] = boa.env.generate_address("unstaged_token")
    assert_order_not_valid(harness, order, "NotAllowed")


def test_lot_window_bounds_invalid(harness, valid_order, sell_token, lot):
    now = _timestamp()
    # Not started yet.
    harness.set_context(sell_token, True, AVAILABLE, INITIAL_AMOUNT, now + 60, lot.end)
    assert_order_not_valid(harness, valid_order, "NotAllowed")

    # Already over: block.timestamp == end is exclusive.
    harness.set_context(sell_token, True, AVAILABLE, INITIAL_AMOUNT, lot.start, now)
    assert_order_not_valid(harness, valid_order, "NotAllowed")


def test_expired_lot_invalid(harness, valid_order, lot):
    signature = encode_bare_signature(valid_order)
    order_hash = order_digest(valid_order)
    assert harness.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE

    boa.env.time_travel(seconds=lot.end - _timestamp())
    with boa.reverts(custom_err("OrderNotValid(string)", "NotAllowed")):
        harness.isValidSignature(order_hash, signature)


def test_zero_available_balance_invalid(harness, valid_order, sell_token, lot):
    harness.set_context(sell_token, True, 0, INITIAL_AMOUNT, lot.start, lot.end)
    assert_order_not_valid(harness, valid_order, "ZeroBalance")


@pytest.mark.parametrize("sell_amount", [0, INITIAL_AMOUNT + 1])
def test_sell_amount_bounds_invalid(harness, valid_order, sell_amount):
    order = deepcopy(valid_order)
    order[ORDER_SELL_AMOUNT] = sell_amount
    assert_order_not_valid(harness, order, "BadSellAmount")


def test_valid_to_bounds(harness, valid_order, lot):
    # validTo may reach exactly the lot end...
    order = deepcopy(valid_order)
    order[ORDER_VALID_TO] = lot.end
    assert (
        harness.isValidSignature(order_digest(order), encode_bare_signature(order))
        == ERC1271_MAGIC_VALUE
    )

    # ...but not beyond it, and never behind the current block.
    order[ORDER_VALID_TO] = lot.end + 1
    assert_order_not_valid(harness, order, "BadValidTo")

    order[ORDER_VALID_TO] = _timestamp() - 1
    assert_order_not_valid(harness, order, "BadValidTo")


def test_expired_valid_to_invalid(harness, valid_order):
    signature = encode_bare_signature(valid_order)
    order_hash = order_digest(valid_order)
    assert harness.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE

    # Travel past validTo while the lot window is still open.
    boa.env.time_travel(seconds=valid_order[ORDER_VALID_TO] - _timestamp() + 1)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadValidTo")):
        harness.isValidSignature(order_hash, signature)


# ERC-1271 validation: economics through the quote hook


def test_underpriced_order_invalid(harness, valid_order):
    order = deepcopy(valid_order)
    order[ORDER_BUY_AMOUNT] = QUOTE - 1
    assert_order_not_valid(harness, order, "BadBuyAmount")


def test_quote_hook_is_consulted_live(harness, valid_order):
    signature = encode_bare_signature(valid_order)
    order_hash = order_digest(valid_order)
    assert harness.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE

    # A published order goes stale the moment the live quote moves above it.
    harness.set_quote(QUOTE + 1)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadBuyAmount")):
        harness.isValidSignature(order_hash, signature)

    # Overpaying relative to the quote is always acceptable.
    harness.set_quote(QUOTE - 1)
    assert harness.isValidSignature(order_hash, signature) == ERC1271_MAGIC_VALUE


# ERC-1271 validation: malformed transport encodings


@pytest.mark.parametrize(
    "mangle",
    [
        lambda signature: b"",
        lambda signature: b"\x00" * 10,
        lambda signature: signature[: ENCODED_ORDER_LEN - 1],  # truncated bare order
        lambda signature: b"\xff" * ENCODED_ORDER_LEN,  # undecodable garbage words
        lambda signature: signature + bytes(32),  # bare order + padding: broken wrapper
    ],
)
def test_malformed_signature_bytes_revert(harness, valid_order, mangle):
    # Anything that is neither a decodable bare order nor a decodable wrapper
    # dies inside abi_decode with a bare revert (no OrderNotValid data).
    signature = mangle(encode_bare_signature(valid_order))
    with boa.reverts():
        harness.isValidSignature(order_digest(valid_order), signature)


def test_padded_wrapper_signature_non_canonical(harness, valid_order):
    # Trailing bytes survive abi_decode of the dynamic wrapper, so the module
    # catches them through re-encoding instead of a raw decode failure.
    handler = boa.env.generate_address("handler")
    signature = encode_wrapper_signature(valid_order, handler) + b"\x00"
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        harness.isValidSignature(order_digest(valid_order), signature)


def test_zero_filled_order_fails_checks_not_decode(harness, lot):
    # 384 zero bytes decode into the empty order: it passes transport, then
    # dies on the first economic check (digest of the empty order mismatch).
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"any hash"), bytes(ENCODED_ORDER_LEN))
