"""Standalone tests for the stateless uniswapx module.

Covers ExclusiveDutchOrder decoding and hashing, the Permit2 witness digest,
and the reactor decay against an independent EIP-712 reference and a vector
produced by @uniswap/uniswapx-sdk (DutchOrder.hash / permitData on chain 1).
The UniswapXAdapter built on top (shape checks and typed reverts) is tested
in test_uniswapx_adapter.py; settlement through the real reactor in
test_uniswapx_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.burners.conftest import custom_err

MAX_UINT256 = 2**256 - 1

ORDER_INFO_TYPE = (
    "OrderInfo(address reactor,address swapper,uint256 nonce,uint256 deadline,"
    "address additionalValidationContract,bytes additionalValidationData)"
)
DUTCH_OUTPUT_TYPE = (
    "DutchOutput(address token,uint256 startAmount,uint256 endAmount,address recipient)"
)
EXCLUSIVE_DUTCH_ORDER_TYPE = (
    "ExclusiveDutchOrder(OrderInfo info,uint256 decayStartTime,uint256 decayEndTime,"
    "address exclusiveFiller,uint256 exclusivityOverrideBps,address inputToken,"
    "uint256 inputStartAmount,uint256 inputEndAmount,DutchOutput[] outputs)"
)
TOKEN_PERMISSIONS_TYPE = "TokenPermissions(address token,uint256 amount)"
PERMIT_WITNESS_STUB = (
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
    "uint256 deadline,"
)

ORDER_INFO_TYPE_HASH = keccak(text=ORDER_INFO_TYPE)
DUTCH_OUTPUT_TYPE_HASH = keccak(text=DUTCH_OUTPUT_TYPE)
ORDER_TYPE_HASH = keccak(text=EXCLUSIVE_DUTCH_ORDER_TYPE + DUTCH_OUTPUT_TYPE + ORDER_INFO_TYPE)
TOKEN_PERMISSIONS_TYPE_HASH = keccak(text=TOKEN_PERMISSIONS_TYPE)
PERMIT_WITNESS_TYPE_HASH = keccak(
    text=PERMIT_WITNESS_STUB
    + "ExclusiveDutchOrder witness)"
    + DUTCH_OUTPUT_TYPE
    + EXCLUSIVE_DUTCH_ORDER_TYPE
    + ORDER_INFO_TYPE
    + TOKEN_PERMISSIONS_TYPE
)

ORDER_INFO_ABI = "(address,address,uint256,uint256,address,bytes)"
DUTCH_INPUT_ABI = "(address,uint256,uint256)"
DUTCH_OUTPUT_ABI = "(address,uint256,uint256,address)"
ORDER_ABI = (
    f"({ORDER_INFO_ABI},uint256,uint256,address,uint256,{DUTCH_INPUT_ABI},{DUTCH_OUTPUT_ABI}[])"
)

# @uniswap/uniswapx-sdk 2.x (DutchOrder = ExclusiveDutchOrder), chain 1:
# `order.serialize()`, `order.hash()`, and ethers' TypedDataEncoder digest of
# `order.permitData()` under the canonical Permit2 domain.
SDK_REACTOR = "0x6000da47483062A0D734Ba3dc7576Ce6A0B645C4"
SDK_PERMIT2_DOMAIN = bytes.fromhex(
    "866a5aba21966af95d6c7ab78eb2b2fc913915c28be3b9aa07cc04ff903e3f28"
)
SDK_ORDER = (
    (
        SDK_REACTOR,
        "0x1111111111111111111111111111111111111111",
        0x0102030405060708091011121314151617181920212223242526272829303132,
        1_800_000_000,
        "0x2222222222222222222222222222222222222222",
        bytes.fromhex("deadbeef"),
    ),
    1_799_000_000,
    1_799_500_000,
    "0x3333333333333333333333333333333333333333",
    25,
    ("0x4444444444444444444444444444444444444444", 250 * 10**18, 250 * 10**18),
    [
        (
            "0x5555555555555555555555555555555555555555",
            100_000 * 10**18,
            10**18,
            "0x6666666666666666666666666666666666666666",
        )
    ],
)
SDK_ENCODED = bytes.fromhex(
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000120"
    "000000000000000000000000000000000000000000000000000000006b3a8fc0"
    "000000000000000000000000000000000000000000000000000000006b4230e0"
    "0000000000000000000000003333333333333333333333333333333333333333"
    "0000000000000000000000000000000000000000000000000000000000000019"
    "0000000000000000000000004444444444444444444444444444444444444444"
    "00000000000000000000000000000000000000000000000d8d726b7177a80000"
    "00000000000000000000000000000000000000000000000d8d726b7177a80000"
    "0000000000000000000000000000000000000000000000000000000000000220"
    "0000000000000000000000006000da47483062a0d734ba3dc7576ce6a0b645c4"
    "0000000000000000000000001111111111111111111111111111111111111111"
    "0102030405060708091011121314151617181920212223242526272829303132"
    "000000000000000000000000000000000000000000000000000000006b49d200"
    "0000000000000000000000002222222222222222222222222222222222222222"
    "00000000000000000000000000000000000000000000000000000000000000c0"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "deadbeef00000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000005555555555555555555555555555555555555555"
    "00000000000000000000000000000000000000000000152d02c7e14af6800000"
    "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
    "0000000000000000000000006666666666666666666666666666666666666666"
)
SDK_ORDER_HASH = bytes.fromhex("c66b4d628eee66fae0ff8e2322a0aaa5d80db9544c5e49761a6a2851c293c7fd")
SDK_PERMIT_TYPE_HASH = bytes.fromhex(
    "2846b6ca8e0ecdbc9ca7696f16bdf77b3baf48504ac14d6a541484ec197e91eb"
)
SDK_DIGEST = bytes.fromhex("00dd5be0f628919c982b5a55de1153567412ee2637d02e135d291e329f2795aa")

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.uniswapx.uniswapx as uniswapx


@external
@pure
def decode(_encoded: Bytes[uniswapx.ENCODED_ORDER_BOUND]) -> uniswapx.ExclusiveDutchOrder:
    return abi_decode(_encoded, uniswapx.ExclusiveDutchOrder)


@external
@pure
def order_hash(_order: uniswapx.ExclusiveDutchOrder) -> bytes32:
    return uniswapx._order_hash(_order)


@external
@pure
def permit_digest(_order: uniswapx.ExclusiveDutchOrder, _domain_separator: bytes32) -> bytes32:
    return uniswapx._permit_digest(_order, _domain_separator)


@external
@view
def decayed_amount(_start: uint256, _end: uint256, _decay_start: uint256, _decay_end: uint256) -> uint256:
    return uniswapx._decayed_amount(_start, _end, _decay_start, _decay_end)


@external
@view
def decayed_output(_output: uniswapx.DutchOutput, _decay_start: uint256, _decay_end: uint256) -> uint256:
    return uniswapx._decayed_output(_output, _decay_start, _decay_end)


@external
@pure
def encoded_order_bound() -> uint256:
    return uniswapx.ENCODED_ORDER_BOUND
"""


def order_hash_reference(order) -> bytes:
    """Independent ExclusiveDutchOrderLib.hash model from eth_abi/keccak."""
    info, decay_start, decay_end, exclusive_filler, override_bps, input_, outputs = order
    info_hash = keccak(
        encode(
            ["bytes32", "address", "address", "uint256", "uint256", "address", "bytes32"],
            [ORDER_INFO_TYPE_HASH, *info[:5], keccak(info[5])],
        )
    )
    output_hashes = b"".join(
        keccak(
            encode(
                ["bytes32", "address", "uint256", "uint256", "address"],
                [DUTCH_OUTPUT_TYPE_HASH, *o],
            )
        )
        for o in outputs
    )
    return keccak(
        encode(
            [
                "bytes32",
                "bytes32",
                "uint256",
                "uint256",
                "address",
                "uint256",
                "address",
                "uint256",
                "uint256",
                "bytes32",
            ],
            [
                ORDER_TYPE_HASH,
                info_hash,
                decay_start,
                decay_end,
                exclusive_filler,
                override_bps,
                *input_,
                keccak(output_hashes),
            ],
        )
    )


def permit_digest_reference(order, domain_separator: bytes) -> bytes:
    """Independent Permit2 PermitHash.hashWithWitness + _hashTypedData model."""
    info, *_rest, input_, _outputs = order
    permitted_hash = keccak(
        encode(
            ["bytes32", "address", "uint256"], [TOKEN_PERMISSIONS_TYPE_HASH, input_[0], input_[2]]
        )
    )
    struct_hash = keccak(
        encode(
            ["bytes32", "bytes32", "address", "uint256", "uint256", "bytes32"],
            [
                PERMIT_WITNESS_TYPE_HASH,
                permitted_hash,
                info[0],
                info[2],
                info[3],
                order_hash_reference(order),
            ],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def decay_reference(start: int, end: int, decay_start: int, decay_end: int, now: int) -> int:
    """DutchDecayLib.decay with solmate mulDivDown."""
    if start == end:
        return start
    assert decay_start < decay_end
    if decay_end <= now:
        return end
    if decay_start >= now:
        return start
    elapsed, duration = now - decay_start, decay_end - decay_start
    if end < start:
        return start - (start - end) * elapsed // duration
    return start + (end - start) * elapsed // duration


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="UniswapXHarness", filename="UniswapXHarness.vy")


# Type hashes and the SDK vector


def test_type_hashes_match_the_sdk_permit_type():
    assert PERMIT_WITNESS_TYPE_HASH == SDK_PERMIT_TYPE_HASH


def test_sdk_vector_decodes_hashes_and_digests(harness):
    decoded = harness.decode(SDK_ENCODED)
    assert bytes(harness.order_hash(decoded)) == SDK_ORDER_HASH
    assert bytes(harness.permit_digest(decoded, SDK_PERMIT2_DOMAIN)) == SDK_DIGEST
    # The reference model agrees with the SDK too, so the fuzz below is
    # anchored to a third-party implementation.
    assert order_hash_reference(SDK_ORDER) == SDK_ORDER_HASH
    assert permit_digest_reference(SDK_ORDER, SDK_PERMIT2_DOMAIN) == SDK_DIGEST
    assert encode([ORDER_ABI], [SDK_ORDER]) == SDK_ENCODED


def test_permit_digest_binds_the_domain(harness):
    other_domain = keccak(b"another permit2 domain")
    assert bytes(harness.permit_digest(SDK_ORDER, other_domain)) == permit_digest_reference(
        SDK_ORDER, other_domain
    )
    assert harness.permit_digest(SDK_ORDER, other_domain) != harness.permit_digest(
        SDK_ORDER, SDK_PERMIT2_DOMAIN
    )


# Decoding bounds


def test_encoded_order_bound_matches_the_abi_layout(harness):
    max_data = bytes(256)
    max_order = (
        (SDK_ORDER[0][0], SDK_ORDER[0][1], 0, 0, SDK_ORDER[0][4], max_data),
        *SDK_ORDER[1:],
    )
    encoded = encode([ORDER_ABI], [max_order])
    assert len(encoded) == harness.encoded_order_bound()
    assert bytes(harness.order_hash(harness.decode(encoded))) == order_hash_reference(max_order)


def test_decode_rejects_oversized_shapes(harness):
    two_outputs = (*SDK_ORDER[:6], [SDK_ORDER[6][0], SDK_ORDER[6][0]])
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [two_outputs]))
    long_data = ((*SDK_ORDER[0][:5], bytes(257)), *SDK_ORDER[1:])
    with boa.reverts():
        harness.decode(encode([ORDER_ABI], [long_data]))


def test_zero_outputs_hash_like_the_reactor(harness):
    no_outputs = (*SDK_ORDER[:6], [])
    assert bytes(harness.order_hash(no_outputs)) == order_hash_reference(no_outputs)


# Hash fuzz against the reference


@given(
    reactor_int=st.integers(min_value=0, max_value=2**160 - 1),
    swapper_int=st.integers(min_value=0, max_value=2**160 - 1),
    nonce=st.integers(min_value=0, max_value=MAX_UINT256),
    deadline=st.integers(min_value=0, max_value=MAX_UINT256),
    validation_int=st.integers(min_value=0, max_value=2**160 - 1),
    validation_data=st.binary(min_size=0, max_size=256),
    decay_start=st.integers(min_value=0, max_value=MAX_UINT256),
    decay_end=st.integers(min_value=0, max_value=MAX_UINT256),
    filler_int=st.integers(min_value=0, max_value=2**160 - 1),
    override_bps=st.integers(min_value=0, max_value=MAX_UINT256),
    input_int=st.integers(min_value=0, max_value=2**160 - 1),
    input_start=st.integers(min_value=0, max_value=MAX_UINT256),
    input_end=st.integers(min_value=0, max_value=MAX_UINT256),
    output_int=st.integers(min_value=0, max_value=2**160 - 1),
    output_start=st.integers(min_value=0, max_value=MAX_UINT256),
    output_end=st.integers(min_value=0, max_value=MAX_UINT256),
    recipient_int=st.integers(min_value=0, max_value=2**160 - 1),
    domain=st.binary(min_size=32, max_size=32),
)
@settings(max_examples=40, deadline=None)
def test_hash_and_digest_fuzz_match_reference(
    harness,
    reactor_int,
    swapper_int,
    nonce,
    deadline,
    validation_int,
    validation_data,
    decay_start,
    decay_end,
    filler_int,
    override_bps,
    input_int,
    input_start,
    input_end,
    output_int,
    output_start,
    output_end,
    recipient_int,
    domain,
):
    order = (
        (
            address_from_int(reactor_int),
            address_from_int(swapper_int),
            nonce,
            deadline,
            address_from_int(validation_int),
            validation_data,
        ),
        decay_start,
        decay_end,
        address_from_int(filler_int),
        override_bps,
        (address_from_int(input_int), input_start, input_end),
        [(address_from_int(output_int), output_start, output_end, address_from_int(recipient_int))],
    )
    decoded = harness.decode(encode([ORDER_ABI], [order]))
    assert bytes(harness.order_hash(decoded)) == order_hash_reference(order)
    assert bytes(harness.permit_digest(decoded, domain)) == permit_digest_reference(order, domain)


# Decay


def _now() -> int:
    return boa.env.evm.vm.state.timestamp


def test_decay_branches_match_the_reactor(harness):
    now = _now()
    # Static amounts ignore the window entirely, inverted windows included.
    assert harness.decayed_amount(7, 7, now + 10, now) == 7
    # Before, inside, and after the window.
    assert harness.decayed_amount(100, 40, now + 10, now + 20) == 100
    assert harness.decayed_amount(100, 40, now - 20, now - 10) == 40
    assert harness.decayed_amount(100, 40, now - 5, now + 5) == 70
    assert harness.decayed_amount(100, 40, now - 5, now + 5) == decay_reference(
        100, 40, now - 5, now + 5, now
    )
    # Rising amounts (reactor inputs) interpolate upward.
    assert harness.decayed_amount(40, 100, now - 5, now + 5) == 70
    # Rounding toward the start amount like mulDivDown.
    assert harness.decayed_amount(100, 0, now - 1, now + 2) == 100 - 100 // 3
    with boa.reverts(custom_err("OrderNotValid(string)", "BadDecayTimes")):
        harness.decayed_amount(100, 40, now + 5, now - 5)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadDecayTimes")):
        harness.decayed_amount(100, 40, now, now)


def test_decayed_output_rejects_rising_outputs(harness):
    now = _now()
    output = (address_from_int(5), 40, 100, address_from_int(6))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadDecayDirection")):
        harness.decayed_output(output, now - 5, now + 5)
    falling = (address_from_int(5), 100, 40, address_from_int(6))
    assert harness.decayed_output(falling, now - 5, now + 5) == 70


@given(
    start=st.integers(min_value=0, max_value=2**128),
    end=st.integers(min_value=0, max_value=2**128),
    start_offset=st.integers(min_value=-1000, max_value=1000),
    duration=st.integers(min_value=1, max_value=2000),
)
@settings(max_examples=60, deadline=None)
def test_decay_fuzz_matches_reference(harness, start, end, start_offset, duration):
    now = _now()
    decay_start = max(now + start_offset, 0)
    decay_end = decay_start + duration
    assert harness.decayed_amount(start, end, decay_start, decay_end) == decay_reference(
        start, end, decay_start, decay_end, now
    )
