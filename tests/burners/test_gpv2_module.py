"""Standalone tests for the stateless gpv2 module.

Covers GPv2 order construction/hashing against an independent EIP-712
reference, flag and balance-mode checks, and the CoW-canonical revert ABI. The
cow_adapter module built on top is tested in test_cow_adapter.py.
"""

from copy import deepcopy

import boa
import pytest
from boa import BoaError
from eth_abi import encode
from eth_hash.auto import keccak
from eth_utils import to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st


DOMAIN_SEPARATOR = keccak(b"test GPv2 settlement domain")
ORDER_TYPE_HASH = bytes.fromhex("d5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489")
SELL_KIND = bytes.fromhex("f3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775")
TOKEN_BALANCE = bytes.fromhex("5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9")
APP_DATA = keccak(b"test app data")
ZERO_BYTES32 = bytes(32)
MAX_UINT256 = 2**256 - 1

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

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.cow.gpv2 as gpv2


@external
@pure
def order_digest(_order: gpv2.GPv2Order, _domain_separator: bytes32) -> bytes32:
    return gpv2._order_digest(_order, _domain_separator)


@external
@pure
def check_order_flags(_order: gpv2.GPv2Order) -> bool:
    return gpv2._check_order_flags(_order)


@external
@pure
def check_balance_modes(_order: gpv2.GPv2Order) -> bool:
    return gpv2._check_balance_modes(_order)


@external
@pure
def order_not_valid(_reason: String[32]):
    raise gpv2.OrderNotValid(reason=_reason)


@external
@pure
def encode_bare_order(_order: gpv2.GPv2Order) -> Bytes[gpv2.ENCODED_ORDER_LEN]:
    return abi_encode(_order)

"""


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def order_digest_reference(order, domain_separator: bytes = DOMAIN_SEPARATOR) -> bytes:
    """Independent EIP-712 digest model built from eth_abi/keccak primitives."""
    struct_hash = keccak(encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


def revert_data(error: BoaError) -> bytes:
    return bytes(error.args[0].output)


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
def harness():
    return boa.loads(
        HARNESS_SOURCE,
        name="Gpv2Harness",
        filename="Gpv2Harness.vy",
        no_vvm=True,
    )


@pytest.fixture(scope="module")
def canonical_order(sell_token, want_token, proceeds_receiver):
    return [
        sell_token,
        want_token,
        proceeds_receiver,
        150 * 10**18,
        11 * 10**18,
        1_800_000_000,
        APP_DATA,
        0,
        SELL_KIND,
        True,
        TOKEN_BALANCE,
        TOKEN_BALANCE,
    ]


# GPv2 order digest


def test_order_digest_matches_eip712_reference(harness, canonical_order):
    digest = harness.order_digest(canonical_order, DOMAIN_SEPARATOR)
    assert bytes(digest) == order_digest_reference(canonical_order)

    other_domain = keccak(b"another settlement domain")
    assert bytes(harness.order_digest(canonical_order, other_domain)) == order_digest_reference(
        canonical_order, other_domain
    )
    assert harness.order_digest(canonical_order, other_domain) != digest


@given(
    sell_token_int=st.integers(min_value=1, max_value=2**160 - 1),
    buy_token_int=st.integers(min_value=1, max_value=2**160 - 1),
    receiver_int=st.integers(min_value=0, max_value=2**160 - 1),
    sell_amount=st.integers(min_value=0, max_value=MAX_UINT256),
    buy_amount=st.integers(min_value=0, max_value=MAX_UINT256),
    valid_to=st.integers(min_value=0, max_value=2**32 - 1),
    app_data=st.binary(min_size=32, max_size=32),
    fee_amount=st.integers(min_value=0, max_value=MAX_UINT256),
    kind=st.binary(min_size=32, max_size=32),
    partially_fillable=st.booleans(),
    sell_balance=st.binary(min_size=32, max_size=32),
    buy_balance=st.binary(min_size=32, max_size=32),
)
@settings(max_examples=50, deadline=None)
def test_order_digest_fuzz_matches_reference(
    harness,
    sell_token_int,
    buy_token_int,
    receiver_int,
    sell_amount,
    buy_amount,
    valid_to,
    app_data,
    fee_amount,
    kind,
    partially_fillable,
    sell_balance,
    buy_balance,
):
    order = [
        address_from_int(sell_token_int),
        address_from_int(buy_token_int),
        address_from_int(receiver_int),
        sell_amount,
        buy_amount,
        valid_to,
        app_data,
        fee_amount,
        kind,
        partially_fillable,
        sell_balance,
        buy_balance,
    ]
    assert bytes(harness.order_digest(order, DOMAIN_SEPARATOR)) == order_digest_reference(order)


# Order flag and balance-mode checks


def test_check_order_flags_accepts_canonical_order(harness, canonical_order):
    assert harness.check_order_flags(canonical_order)
    assert harness.check_balance_modes(canonical_order)


@pytest.mark.parametrize(
    "index,value",
    [
        (7, 1),  # feeAmount != 0
        (7, MAX_UINT256),
        (8, TOKEN_BALANCE),  # kind != SELL_KIND
        (8, ZERO_BYTES32),
        (9, False),  # not partiallyFillable
    ],
)
def test_check_order_flags_rejects_each_violation(harness, canonical_order, index, value):
    order = deepcopy(canonical_order)
    order[index] = value
    assert not harness.check_order_flags(order)
    # Balance modes are a separate check so the watchtower keeps its
    # BadOrderFlags-versus-BadBalanceMode error granularity.
    assert harness.check_balance_modes(order)


@pytest.mark.parametrize(
    "index,value",
    [
        (10, SELL_KIND),  # sellTokenBalance != erc20
        (10, ZERO_BYTES32),
        (11, SELL_KIND),  # buyTokenBalance != erc20
        (11, ZERO_BYTES32),
    ],
)
def test_check_balance_modes_rejects_each_violation(harness, canonical_order, index, value):
    order = deepcopy(canonical_order)
    order[index] = value
    assert not harness.check_balance_modes(order)
    assert harness.check_order_flags(order)


def test_check_order_flags_ignores_economic_fields(harness, canonical_order):
    # Economics are the core's responsibility; the flag checks are layout-only.
    order = deepcopy(canonical_order)
    order[3] = 0
    order[4] = 0
    order[5] = 0
    assert harness.check_order_flags(order)
    assert harness.check_balance_modes(order)


# CoW-canonical revert ABI


def test_order_not_valid_revert_data(harness):
    with pytest.raises(BoaError) as error:
        harness.order_not_valid("BadStaticInput")
    assert revert_data(error.value) == selector("OrderNotValid(string)") + encode(
        ["string"], ["BadStaticInput"]
    )


# Signature shape invariants for the router


def test_bare_order_payload_never_aliases_a_verifier_prefix(harness, canonical_order):
    # The bare CoW encoding starts with the ABI zero padding of the sellToken
    # address head: its first 12 bytes are zero, so an unprefixed order can
    # never be mistaken for a 20-byte verifier prefix by the router — and a
    # publisher always prepends the CowAdapter address explicitly.
    worst_case_order = deepcopy(canonical_order)
    worst_case_order[0] = to_checksum_address(b"\xff" * 20)
    for order in (canonical_order, worst_case_order):
        bare = bytes(harness.encode_bare_order(order))
        assert len(bare) == 12 * 32
        assert bare[:12] == bytes(12)
