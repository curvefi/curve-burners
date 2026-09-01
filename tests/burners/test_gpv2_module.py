"""Standalone tests for the stateless gpv2 module.

Covers GPv2 order construction/hashing against an independent EIP-712
reference, flag and balance-mode checks, quote/validity bucketing, the
conditional-order static-input codec, and the watchtower revert ABI. The
deployable CowAdapter built on top is tested in test_cow_adapter.py.
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
STATIC_INPUT_LEN = 52

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
def build_sell_order(
    _sell_token: address,
    _buy_token: address,
    _receiver: address,
    _sell_amount: uint256,
    _buy_amount: uint256,
    _valid_to: uint32,
    _app_data: bytes32,
) -> gpv2.GPv2Order:
    return gpv2._build_sell_order(
        _sell_token, _buy_token, _receiver, _sell_amount, _buy_amount, _valid_to, _app_data
    )


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
def bucket_quote_time(_timestamp: uint256, _start: uint256, _validity: uint256) -> uint256:
    return gpv2._bucket_quote_time(_timestamp, _start, _validity)


@external
@pure
def bucket_valid_to(_timestamp: uint256, _end: uint256, _validity: uint256) -> uint32:
    return gpv2._bucket_valid_to(_timestamp, _end, _validity)


@external
@pure
def encode_static_input(
    _token: address, _generation: uint256
) -> Bytes[gpv2.STATIC_INPUT_LEN]:
    return gpv2._encode_static_input(_token, _generation)


@external
@pure
def decode_static_input(
    _static_input: Bytes[gpv2.MAX_HANDLER_INPUT_LEN],
) -> (bool, address, uint256):
    return gpv2._decode_static_input(_static_input)


@external
@pure
def order_not_valid(_reason: String[32]):
    raise gpv2.OrderNotValid(reason=_reason)


@external
@pure
def poll_try_at(_timestamp: uint256, _reason: String[32]):
    raise gpv2.PollTryAtEpoch(timestamp=_timestamp, reason=_reason)


@external
@pure
def encode_legacy_signature(
    _order: gpv2.GPv2Order, _payload: gpv2.PayloadStruct
) -> Bytes[4096]:
    return abi_encode(_order, _payload)

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


# Canonical sell order construction


def test_build_sell_order_sets_canonical_fields(
    harness, canonical_order, sell_token, want_token, proceeds_receiver
):
    order = harness.build_sell_order(
        sell_token,
        want_token,
        proceeds_receiver,
        150 * 10**18,
        11 * 10**18,
        1_800_000_000,
        APP_DATA,
    )
    assert list(order) == canonical_order
    assert harness.check_order_flags(order)
    assert harness.check_balance_modes(order)
    assert bytes(harness.order_digest(order, DOMAIN_SEPARATOR)) == order_digest_reference(
        canonical_order
    )


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


# Quote-time and validity buckets


def test_bucket_quote_time_clamps_to_start(harness):
    validity = 120
    start = 1_000_030
    # Timestamps before the first full bucket boundary stay pinned to start.
    assert harness.bucket_quote_time(start, start, validity) == start
    assert harness.bucket_quote_time(1_000_079, start, validity) == start
    # From the next boundary on, the bucket start wins.
    assert harness.bucket_quote_time(1_000_080, start, validity) == 1_000_080


def test_bucket_quote_time_is_stable_inside_a_bucket(harness):
    validity = 120
    start = 999_960
    bucket_start = 1_000_080
    for timestamp in (bucket_start, bucket_start + 1, bucket_start + validity - 1):
        assert harness.bucket_quote_time(timestamp, start, validity) == bucket_start
    assert harness.bucket_quote_time(bucket_start + validity, start, validity) == (
        bucket_start + validity
    )


def test_bucket_valid_to_is_stable_and_switches_on_boundary(harness):
    validity = 120
    end = 2_000_000
    bucket_start = 1_000_080
    for timestamp in (bucket_start, bucket_start + 1, bucket_start + validity - 1):
        assert harness.bucket_valid_to(timestamp, end, validity) == bucket_start + validity
    assert harness.bucket_valid_to(bucket_start + validity, end, validity) == (
        bucket_start + 2 * validity
    )


def test_bucket_valid_to_caps_at_auction_end(harness):
    validity = 120
    bucket_start = 1_000_080
    # An end inside the current bucket truncates validTo to the end itself.
    assert harness.bucket_valid_to(bucket_start + 1, bucket_start + 60, validity) == (
        bucket_start + 60
    )
    assert harness.bucket_valid_to(bucket_start + 1, bucket_start + validity, validity) == (
        bucket_start + validity
    )


def test_bucket_valid_to_reverts_beyond_uint32(harness):
    with boa.reverts():
        harness.bucket_valid_to(2**32, 2**33, 120)


@given(
    timestamp=st.integers(min_value=0, max_value=2**32 - 2),
    start=st.integers(min_value=0, max_value=2**32 - 2),
    end=st.integers(min_value=0, max_value=2**32 - 1),
    validity=st.integers(min_value=1, max_value=2**20),
)
@settings(max_examples=50, deadline=None)
def test_bucket_fuzz_matches_model(harness, timestamp, start, end, validity):
    assert harness.bucket_quote_time(timestamp, start, validity) == max(
        timestamp // validity * validity, start
    )
    expected_valid_to = min((timestamp // validity + 1) * validity, end)
    assert harness.bucket_valid_to(timestamp, end, validity) == expected_valid_to


# Conditional-order static input codec


def test_static_input_roundtrip(harness, sell_token):
    generation = 7
    encoded = bytes(harness.encode_static_input(sell_token, generation))
    assert len(encoded) == STATIC_INPUT_LEN
    assert encoded == bytes.fromhex(sell_token[2:]) + generation.to_bytes(32, "big")

    ok, token, decoded_generation = harness.decode_static_input(encoded)
    assert ok
    assert token == sell_token
    assert decoded_generation == generation


@given(generation=st.integers(min_value=0, max_value=MAX_UINT256))
@settings(max_examples=50, deadline=None)
def test_static_input_roundtrip_fuzz(harness, sell_token, generation):
    ok, token, decoded_generation = harness.decode_static_input(
        harness.encode_static_input(sell_token, generation)
    )
    assert ok
    assert token == sell_token
    assert decoded_generation == generation


@pytest.mark.parametrize(
    "garbage",
    [
        b"",
        b"\x01",
        b"\x01" * (STATIC_INPUT_LEN - 1),
        b"\x01" * (STATIC_INPUT_LEN + 1),
        b"\x01" * 256,
        bytes(STATIC_INPUT_LEN),  # zero token address
        bytes(20) + (1).to_bytes(32, "big"),  # zero token, nonzero generation
    ],
)
def test_static_input_garbage_is_rejected_without_revert(harness, garbage):
    ok, token, generation = harness.decode_static_input(garbage)
    assert not ok
    assert token == "0x0000000000000000000000000000000000000000"
    assert generation == 0


# Watchtower error helpers


def test_order_not_valid_revert_data(harness):
    with pytest.raises(BoaError) as error:
        harness.order_not_valid("BadStaticInput")
    assert revert_data(error.value) == selector("OrderNotValid(string)") + encode(
        ["string"], ["BadStaticInput"]
    )


def test_poll_try_at_revert_data(harness):
    with pytest.raises(BoaError) as error:
        harness.poll_try_at(1_800_000_000, "NotAllowed")
    assert revert_data(error.value) == selector("PollTryAtEpoch(uint256,string)") + encode(
        ["uint256", "string"], [1_800_000_000, "NotAllowed"]
    )


# Signature shape invariants for the router


def test_legacy_composable_signature_never_aliases_a_verifier_prefix(harness, canonical_order):
    # Both historical CoW encodings start with the ABI zero padding of the
    # sellToken address head: their first 12 bytes are zero, so the 20-byte
    # routing prefix can never equal a deployed verifier address and the
    # shape router stays unambiguous.
    payload = (
        [keccak(b"proof")],
        (boa.env.generate_address("handler"), ZERO_BYTES32, b"\xee" * STATIC_INPUT_LEN),
        b"\xdd" * 8,
    )
    worst_case_order = deepcopy(canonical_order)
    worst_case_order[0] = to_checksum_address(b"\xff" * 20)

    for order in (canonical_order, worst_case_order):
        legacy = bytes(harness.encode_legacy_signature(order, payload))
        assert legacy[:12] == bytes(12)
