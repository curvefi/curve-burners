"""Standalone tests for the stateless gpv2 module.

Covers GPv2 order construction/hashing against an independent EIP-712
reference. The CowAdapter built on top (flag, balance-mode, and typed-error
reverts) is tested in test_cow_adapter.py.
"""

import boa
import pytest
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

import contracts.burners.adapters.cow.gpv2 as gpv2


@external
@pure
def order_digest(_order: gpv2.GPv2Order, _domain_separator: bytes32) -> bytes32:
    return gpv2._order_digest(_order, _domain_separator)
"""


def order_digest_reference(order, domain_separator: bytes = DOMAIN_SEPARATOR) -> bytes:
    """Independent EIP-712 digest model built from eth_abi/keccak primitives."""
    struct_hash = keccak(encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


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
