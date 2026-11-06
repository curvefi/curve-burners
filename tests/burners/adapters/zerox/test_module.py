"""Standalone tests for the stateless 0x settler module.

Covers the Order encoding, the Settler's action-list hash, both witness
shapes (SettlerMetaTxn SlippageAndActions, SettlerIntent Slippage) and the
Permit2 witness digest against an independent EIP-712 reference, the
0x-settler Solidity constants, and a real SettlerIntent fill decoded from
mainnet. The ZeroExSettlerAdapter built on top (shape checks and typed
reverts) is tested in test_zerox_adapter.py; settlement through the real
Settlers in test_zerox_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

MAX_UINT256 = 2**256 - 1

SLIPPAGE_AND_ACTIONS_TYPE = (
    "SlippageAndActions(address recipient,address buyToken,uint256 minAmountOut,bytes[] actions)"
)
SLIPPAGE_TYPE = "Slippage(address recipient,address buyToken,uint256 minAmountOut)"
TOKEN_PERMISSIONS_TYPE = "TokenPermissions(address token,uint256 amount)"
PERMIT_WITNESS_STUB = (
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
    "uint256 deadline,"
)

SLIPPAGE_AND_ACTIONS_TYPE_HASH = keccak(text=SLIPPAGE_AND_ACTIONS_TYPE)
SLIPPAGE_TYPE_HASH = keccak(text=SLIPPAGE_TYPE)
TOKEN_PERMISSIONS_TYPE_HASH = keccak(text=TOKEN_PERMISSIONS_TYPE)
# Permit2PaymentMetaTxn._witnessTypeSuffix / Permit2PaymentIntent._witnessTypeSuffix
# appended to Permit2's _PERMIT_TRANSFER_FROM_WITNESS_TYPEHASH_STUB.
PERMIT_WITNESS_METATXN_TYPE_HASH = keccak(
    text=PERMIT_WITNESS_STUB
    + "SlippageAndActions slippageAndActions)"
    + SLIPPAGE_AND_ACTIONS_TYPE
    + TOKEN_PERMISSIONS_TYPE
)
PERMIT_WITNESS_INTENT_TYPE_HASH = keccak(
    text=PERMIT_WITNESS_STUB + "Slippage slippage)" + SLIPPAGE_TYPE + TOKEN_PERMISSIONS_TYPE
)

PERMIT_ABI = "(address,uint256,uint256,uint256)"
SLIPPAGE_ABI = "(address,address,uint256)"
ORDER_ABI = f"({PERMIT_ABI},{SLIPPAGE_ABI},bytes32)"
ENCODED_ORDER_LEN = 8 * 32

# Permit2's EIP-712 domain on chain 1 (name "Permit2", verifying contract
# 0x000000000022D473030F116dDEE9F6B43aC78BA3).
MAINNET_PERMIT2_DOMAIN = bytes.fromhex(
    "866a5aba21966af95d6c7ab78eb2b2fc913915c28be3b9aa07cc04ff903e3f28"
)

# A real SettlerIntent fill: mainnet tx
# 0xe0bb23a247172417e7ea1349b65790d11dbd3e7f454e4aa0bf7454a7dbf6d391 (block
# 26328545) on SettlerIntent 0xe6e49e…, executeMetaTxn by solver 0x75cddbbb…
# for taker 0x2447ff8c… (an EIP-7702 account; Permit2 took the ERC-1271
# branch). The witness is the slippage only; the digest below is the one the
# taker's 65-byte signature recovers to the taker.
INTENT_TX_SETTLER = "0xe6e49e792dc5727a1dd3450d9834c7058c6d72ce"
INTENT_TX_ORDER = (
    (
        "0xaca92e438df0b2401ff60da7e4337b687a2435da",
        38_162_460_000,
        2241959297937691820908574931994577,
        1793640047,
    ),
    (
        "0x2447ff8c28cad702d74b7ba9428bafd56ae7ebc7",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        38_158_682_777,
    ),
    bytes(32),
)
INTENT_TX_DIGEST = bytes.fromhex("b5077fee91a182040daf70d6c2cb0b3c44b73e933196d55f43a698235d54dd21")

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.zerox.settler as settler


@external
@pure
def decode(_encoded: Bytes[settler.ENCODED_ORDER_LEN]) -> settler.Order:
    return abi_decode(_encoded, settler.Order)


@external
@pure
def hash_actions(_actions: DynArray[Bytes[settler.MAX_ACTION_LEN], settler.MAX_ACTIONS]) -> bytes32:
    return settler._hash_actions(_actions)


@external
@pure
def witness_hash(_order: settler.Order, _intent: bool) -> bytes32:
    return settler._witness_hash(_order, _intent)


@external
@pure
def permit_digest(
    _order: settler.Order, _spender: address, _domain_separator: bytes32, _intent: bool
) -> bytes32:
    return settler._permit_digest(_order, _spender, _domain_separator, _intent)


@external
@pure
def type_hashes() -> (bytes32, bytes32, bytes32, bytes32):
    return (
        settler.SLIPPAGE_AND_ACTIONS_TYPE_HASH,
        settler.SLIPPAGE_TYPE_HASH,
        settler.PERMIT_WITNESS_METATXN_TYPE_HASH,
        settler.PERMIT_WITNESS_INTENT_TYPE_HASH,
    )


@external
@pure
def bounds() -> (uint256, uint256, uint256):
    return settler.ENCODED_ORDER_LEN, settler.MAX_ACTIONS, settler.MAX_ACTION_LEN
"""


def hash_actions_reference(actions: list[bytes]) -> bytes:
    """SettlerMetaTxn._hashArrayOfBytes: keccak of the packed action keccaks."""
    return keccak(b"".join(keccak(action) for action in actions))


def witness_reference(order, intent: bool) -> bytes:
    _permit, slippage, actions_hash = order
    if intent:
        return keccak(
            encode(["bytes32", "address", "address", "uint256"], [SLIPPAGE_TYPE_HASH, *slippage])
        )
    return keccak(
        encode(
            ["bytes32", "address", "address", "uint256", "bytes32"],
            [SLIPPAGE_AND_ACTIONS_TYPE_HASH, *slippage, actions_hash],
        )
    )


def permit_digest_reference(order, spender: str, domain_separator: bytes, intent: bool) -> bytes:
    """Independent Permit2 PermitHash.hashWithWitness + _hashTypedData model."""
    permit, _slippage, _actions_hash = order
    permitted_hash = keccak(
        encode(["bytes32", "address", "uint256"], [TOKEN_PERMISSIONS_TYPE_HASH, *permit[:2]])
    )
    type_hash = PERMIT_WITNESS_INTENT_TYPE_HASH if intent else PERMIT_WITNESS_METATXN_TYPE_HASH
    struct_hash = keccak(
        encode(
            ["bytes32", "bytes32", "address", "uint256", "uint256", "bytes32"],
            [
                type_hash,
                permitted_hash,
                spender,
                permit[2],
                permit[3],
                witness_reference(order, intent),
            ],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def address_from_int(value: int) -> str:
    return to_checksum_address(value.to_bytes(20, "big"))


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="ZeroExHarness", filename="ZeroExHarness.vy")


# Constants


def test_type_hashes_match_the_settler_and_permit2(harness):
    slippage_and_actions, slippage, metatxn, intent = (bytes(h) for h in harness.type_hashes())
    # SettlerAbstract.SLIPPAGE_AND_ACTIONS_TYPEHASH / SLIPPAGE_TYPEHASH literals.
    assert slippage_and_actions == bytes.fromhex(
        "615e8d716cef7295e75dd3f1f10d679914ad6d7759e8e9459f0109ef75241701"
    )
    assert slippage == bytes.fromhex(
        "dc83993a2ffc65b01b71ed08790b6e39c5c55d76937b62a3b5085b02071f1259"
    )
    assert metatxn == PERMIT_WITNESS_METATXN_TYPE_HASH
    assert intent == PERMIT_WITNESS_INTENT_TYPE_HASH


def test_bounds_match_the_abi_layout(harness):
    encoded_len, max_actions, max_action_len = harness.bounds()
    assert encoded_len == ENCODED_ORDER_LEN == len(encode([ORDER_ABI], [INTENT_TX_ORDER]))
    # Sized above the largest fills seen in Settler traffic.
    assert max_actions >= 40 and max_action_len >= 7908


# Real fill vector


def test_real_intent_fill_digest(harness):
    decoded = harness.decode(encode([ORDER_ABI], [INTENT_TX_ORDER]))
    assert (
        bytes(harness.permit_digest(decoded, INTENT_TX_SETTLER, MAINNET_PERMIT2_DOMAIN, True))
        == INTENT_TX_DIGEST
    )
    assert (
        permit_digest_reference(INTENT_TX_ORDER, INTENT_TX_SETTLER, MAINNET_PERMIT2_DOMAIN, True)
        == INTENT_TX_DIGEST
    )
    # The same order under the SettlerMetaTxn witness is a different digest.
    assert (
        bytes(harness.permit_digest(decoded, INTENT_TX_SETTLER, MAINNET_PERMIT2_DOMAIN, False))
        != INTENT_TX_DIGEST
    )


# Action hashing


def test_hash_actions_matches_the_settler(harness):
    actions = [bytes.fromhex("0dfeb419") + bytes(64), b"\x38\xc9\xc1\x47" + b"\x01" * 100]
    assert bytes(harness.hash_actions(actions)) == hash_actions_reference(actions)
    assert bytes(harness.hash_actions([])) == keccak(b"")
    # Actions hash as bytes: a selector-less action hashes like any other
    # (the Settler, not the module, rejects it).
    assert bytes(harness.hash_actions([b"\x01"])) == hash_actions_reference([b"\x01"])


def test_hash_actions_at_the_bounds(harness):
    _, max_actions, max_action_len = harness.bounds()
    actions = [bytes([i % 256]) * max_action_len for i in range(max_actions)]
    assert bytes(harness.hash_actions(actions)) == hash_actions_reference(actions)
    with boa.reverts():
        harness.hash_actions(actions + [b"\x00" * 4])
    with boa.reverts():
        harness.hash_actions([b"\x00" * (max_action_len + 1)])


# Digest fuzz against the reference


@given(
    token_int=st.integers(min_value=0, max_value=2**160 - 1),
    amount=st.integers(min_value=0, max_value=MAX_UINT256),
    nonce=st.integers(min_value=0, max_value=MAX_UINT256),
    deadline=st.integers(min_value=0, max_value=MAX_UINT256),
    recipient_int=st.integers(min_value=0, max_value=2**160 - 1),
    buy_token_int=st.integers(min_value=0, max_value=2**160 - 1),
    min_amount_out=st.integers(min_value=0, max_value=MAX_UINT256),
    actions=st.lists(st.binary(min_size=0, max_size=64), min_size=0, max_size=6),
    spender_int=st.integers(min_value=0, max_value=2**160 - 1),
    domain=st.binary(min_size=32, max_size=32),
    intent=st.booleans(),
)
@settings(max_examples=40, deadline=None)
def test_digest_fuzz_matches_reference(
    harness,
    token_int,
    amount,
    nonce,
    deadline,
    recipient_int,
    buy_token_int,
    min_amount_out,
    actions,
    spender_int,
    domain,
    intent,
):
    actions_hash = bytes(32) if intent else hash_actions_reference(actions)
    order = (
        (address_from_int(token_int), amount, nonce, deadline),
        (address_from_int(recipient_int), address_from_int(buy_token_int), min_amount_out),
        actions_hash,
    )
    spender = address_from_int(spender_int)
    decoded = harness.decode(encode([ORDER_ABI], [order]))
    assert bytes(harness.hash_actions(actions)) == hash_actions_reference(actions)
    assert bytes(harness.witness_hash(decoded, intent)) == witness_reference(order, intent)
    assert bytes(harness.permit_digest(decoded, spender, domain, intent)) == (
        permit_digest_reference(order, spender, domain, intent)
    )
