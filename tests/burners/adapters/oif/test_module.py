"""Standalone tests for the stateless oif module.

Covers the OIF StandardOrder and MandateOutput types, the MandateOutput and
Permit2Witness struct hashes and the Permit2 PermitBatchWitnessTransferFrom
digest the escrow input settler asks the sponsor to sign, against an
independent reference built from eth_abi/keccak primitives (the mainnet
escrow's own orderIdentifier is reproduced the same way as a cross-check of
the type layout), the encoding round trip and bounds, the identifier
helpers and the output settler's context shapes. The OifEscrowAdapter built
on top (shape checks and typed reverts) is tested in test_oif_adapter.py;
settlement through the real protocol in test_oif_fork.py.
"""

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address
from hypothesis import given, settings
from hypothesis import strategies as st

MAX_UINT256 = 2**256 - 1
MASK160 = 2**160 - 1
MAX_INPUTS = 1
MAX_OUTPUTS = 1
MAX_CALLBACK_DATA = 64
MAX_CONTEXT = 64

MANDATE_OUTPUT_TYPE = (
    "MandateOutput(bytes32 oracle,bytes32 settler,uint256 chainId,bytes32 token,"
    "uint256 amount,bytes32 recipient,bytes callbackData,bytes context)"
)
PERMIT2_WITNESS_STUB = (
    "Permit2Witness(address user,uint32 expires,address inputOracle,MandateOutput[] outputs)"
)
TOKEN_PERMISSIONS_TYPE = "TokenPermissions(address token,uint256 amount)"
# Permit2WitnessType.PERMIT2_PERMIT2_TYPESTRING, appended by Permit2 to its
# batch witness stub.
PERMIT2_PERMIT2_TYPESTRING = (
    "Permit2Witness witness)" + MANDATE_OUTPUT_TYPE + PERMIT2_WITNESS_STUB + TOKEN_PERMISSIONS_TYPE
)
PERMIT_BATCH_WITNESS_STUB = (
    "PermitBatchWitnessTransferFrom(TokenPermissions[] permitted,address spender,"
    "uint256 nonce,uint256 deadline,"
)
MANDATE_OUTPUT_TYPE_HASH = keccak(text=MANDATE_OUTPUT_TYPE)
PERMIT2_WITNESS_TYPE_HASH = keccak(text=PERMIT2_WITNESS_STUB + MANDATE_OUTPUT_TYPE)
TOKEN_PERMISSIONS_TYPE_HASH = keccak(text=TOKEN_PERMISSIONS_TYPE)
PERMIT_BATCH_WITNESS_TYPE_HASH = keccak(text=PERMIT_BATCH_WITNESS_STUB + PERMIT2_PERMIT2_TYPESTRING)

MANDATE_ABI = "(bytes32,bytes32,uint256,bytes32,uint256,bytes32,bytes,bytes)"
ORDER_ABI = f"(address,uint256,uint256,uint32,uint32,address,uint256[2][],{MANDATE_ABI}[])"

# Mainnet deployments (Sourcify/Etherscan verified): LI.FI's escrow input
# settler (InputSettlerEscrowLIFI) and the output settler it attests with
# (OutputSettlerSimple), the canonical Permit2 and its mainnet domain
# separator (eth_call DOMAIN_SEPARATOR). The escrow's orderIdentifier for
# PROBE_ORDER was recorded by eth_call and is reproduced below.
LIFI_INPUT_SETTLER = to_checksum_address("0x00fC00edbe7C003b006f870068c548940000223e")
LIFI_OUTPUT_SETTLER = to_checksum_address("0x75220B7600c300005038432a0000f308e0000068")
OIF_INPUT_SETTLER = to_checksum_address("0x1CC9260E285C2C8AC8D2E7102F3978056Ec1d0a8")
PERMIT2 = to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3")
PERMIT2_MAINNET_DOMAIN = bytes.fromhex(
    "866a5aba21966af95d6c7ab78eb2b2fc913915c28be3b9aa07cc04ff903e3f28"
)


def identifier(account: str) -> bytes:
    return int(account, 16).to_bytes(32, "big")


OUTPUT_SETTLER_ID = identifier(LIFI_OUTPUT_SETTLER)
PROBE_ORDER = (
    to_checksum_address("0x1111111111111111111111111111111111111111"),
    0x5AFE,
    1,
    1_900_000_000,
    1_899_999_000,
    LIFI_OUTPUT_SETTLER,
    [[int("0x4444444444444444444444444444444444444444", 16), 250 * 10**18]],
    [
        (
            OUTPUT_SETTLER_ID,
            OUTPUT_SETTLER_ID,
            1,
            identifier("0x5555555555555555555555555555555555555555"),
            100_000 * 10**18,
            identifier("0x2222222222222222222222222222222222222222"),
            b"",
            b"",
        )
    ],
)
PROBE_ORDER_IDS = {
    LIFI_INPUT_SETTLER: bytes.fromhex(
        "10adbf5803671f8507e9747654422315530d0807c6c644739dcc0a7f555eb680"
    ),
    OIF_INPUT_SETTLER: bytes.fromhex(
        "bb176eabb47a1dffe63a914c4ef4e54407a10026f67f609ab4d8111b023f95f2"
    ),
}

HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.adapters.oif.oif as oif


@external
@pure
def output_hash(_output: oif.MandateOutput) -> bytes32:
    return oif._output_hash(_output)


@external
@pure
def witness_hash(_order: oif.StandardOrder) -> bytes32:
    return oif._witness_hash(_order)


@external
@pure
def permit_digest(_order: oif.StandardOrder, _spender: address, _domain: bytes32) -> bytes32:
    return oif._permit_digest(_order, _spender, _domain)


@external
@pure
def encode(_order: oif.StandardOrder) -> Bytes[oif.ENCODED_ORDER_BOUND]:
    return abi_encode(_order)


@external
@pure
def decode(_encoded: Bytes[oif.ENCODED_ORDER_BOUND]) -> oif.StandardOrder:
    return abi_decode(_encoded, oif.StandardOrder)


@external
@pure
def to_identifier(_account: address) -> bytes32:
    return oif._to_identifier(_account)


@external
@pure
def is_clean(_identifier: uint256) -> bool:
    return oif._is_clean(_identifier)


@external
@pure
def from_identifier(_identifier: uint256) -> address:
    return oif._from_identifier(_identifier)


@external
@pure
def resolves_to_signed_amount(_context: Bytes[oif.MAX_CONTEXT]) -> bool:
    return oif._resolves_to_signed_amount(_context)


@external
@pure
def constants() -> (bytes32, bytes32, bytes32, bytes32, uint256, uint256, uint256, uint256, uint256):
    return (
        oif.MANDATE_OUTPUT_TYPE_HASH,
        oif.PERMIT2_WITNESS_TYPE_HASH,
        oif.TOKEN_PERMISSIONS_TYPE_HASH,
        oif.PERMIT_BATCH_WITNESS_TYPE_HASH,
        oif.ENCODED_ORDER_BOUND,
        oif.MAX_INPUTS,
        oif.MAX_OUTPUTS,
        oif.MAX_CALLBACK_DATA,
        oif.MAX_CONTEXT,
    )
"""


def output_hash_reference(output: tuple) -> bytes:
    oracle, settler, chain_id, token, amount, recipient, callback, context = output
    return keccak(
        encode(
            ["bytes32"] * 3 + ["uint256", "bytes32", "uint256", "bytes32", "bytes32", "bytes32"],
            [
                MANDATE_OUTPUT_TYPE_HASH,
                oracle,
                settler,
                chain_id,
                token,
                amount,
                recipient,
                keccak(callback),
                keccak(context),
            ],
        )
    )


def witness_hash_reference(order: tuple) -> bytes:
    user, _nonce, _chain, expires, _fill_deadline, input_oracle, _inputs, outputs = order
    outputs_hash = keccak(b"".join(output_hash_reference(o) for o in outputs))
    return keccak(
        encode(
            ["bytes32", "address", "uint32", "address", "bytes32"],
            [PERMIT2_WITNESS_TYPE_HASH, user, expires, input_oracle, outputs_hash],
        )
    )


def permit_digest_reference(order: tuple, spender: str, domain: bytes) -> bytes:
    """Permit2 PermitHash.hashWithWitness for the escrow's batch permit."""
    permitted = b"".join(
        keccak(
            encode(
                ["bytes32", "address", "uint256"],
                [
                    TOKEN_PERMISSIONS_TYPE_HASH,
                    to_checksum_address((token & MASK160).to_bytes(20, "big")),
                    amount,
                ],
            )
        )
        for token, amount in order[6]
    )
    struct_hash = keccak(
        encode(
            ["bytes32", "bytes32", "address", "uint256", "uint256", "bytes32"],
            [
                PERMIT_BATCH_WITNESS_TYPE_HASH,
                keccak(permitted),
                spender,
                order[1],
                order[4],
                witness_hash_reference(order),
            ],
        )
    )
    return keccak(b"\x19\x01" + domain + struct_hash)


def order_identifier_reference(order: tuple, settler: str, chain_id: int = 1) -> bytes:
    """StandardOrderType.orderIdentifier (abi.encodePacked layout)."""
    inputs_packed = b"".join(
        token.to_bytes(32, "big") + amount.to_bytes(32, "big") for token, amount in order[6]
    )
    return keccak(
        chain_id.to_bytes(32, "big")
        + bytes.fromhex(settler[2:])
        + bytes.fromhex(order[0][2:])
        + order[1].to_bytes(32, "big")
        + order[3].to_bytes(4, "big")
        + order[4].to_bytes(4, "big")
        + bytes.fromhex(order[5][2:])
        + keccak(inputs_packed)
        + encode([f"{MANDATE_ABI}[]"], [order[7]])
    )


@pytest.fixture(scope="module")
def harness():
    return boa.loads(HARNESS_SOURCE, name="OifHarness")


def _normalize(order) -> tuple:
    """The order as the harness returns it (bytes fields as bytes)."""
    order = tuple(order)
    outputs = [
        (bytes(o[0]), bytes(o[1]), o[2], bytes(o[3]), o[4], bytes(o[5]), bytes(o[6]), bytes(o[7]))
        for o in order[7]
    ]
    return (*order[:6], [list(pair) for pair in order[6]], outputs)


# Constants and hashing


def test_constants_match_protocol(harness):
    (
        output_type_hash,
        witness_type_hash,
        permissions_type_hash,
        batch_type_hash,
        bound,
        max_inputs,
        max_outputs,
        max_callback,
        max_context,
    ) = harness.constants()
    assert bytes(output_type_hash) == MANDATE_OUTPUT_TYPE_HASH
    assert bytes(witness_type_hash) == PERMIT2_WITNESS_TYPE_HASH
    assert bytes(permissions_type_hash) == TOKEN_PERMISSIONS_TYPE_HASH
    assert bytes(batch_type_hash) == PERMIT_BATCH_WITNESS_TYPE_HASH
    assert (max_inputs, max_outputs, max_callback, max_context) == (
        MAX_INPUTS,
        MAX_OUTPUTS,
        MAX_CALLBACK_DATA,
        MAX_CONTEXT,
    )
    # The bound is exactly the encoder's output at the field bounds.
    full = (
        *PROBE_ORDER[:7],
        [(*PROBE_ORDER[7][0][:6], b"\x01" * MAX_CALLBACK_DATA, b"\x02" * MAX_CONTEXT)],
    )
    assert len(encode([ORDER_ABI], [full])) == bound


def test_mainnet_order_identifier_reproduced():
    # The escrows' orderIdentifier(PROBE_ORDER), recorded by eth_call, is the
    # packed layout the reference models: the type layout matches.
    for settler, order_id in PROBE_ORDER_IDS.items():
        assert order_identifier_reference(PROBE_ORDER, settler) == order_id


def test_hashes_match_reference(harness):
    output = PROBE_ORDER[7][0]
    assert bytes(harness.output_hash(output)) == output_hash_reference(output)
    assert bytes(harness.witness_hash(PROBE_ORDER)) == witness_hash_reference(PROBE_ORDER)
    digest = harness.permit_digest(PROBE_ORDER, LIFI_INPUT_SETTLER, PERMIT2_MAINNET_DOMAIN)
    assert bytes(digest) == permit_digest_reference(
        PROBE_ORDER, LIFI_INPUT_SETTLER, PERMIT2_MAINNET_DOMAIN
    )
    # Another spender (escrow) or domain is another digest.
    assert bytes(
        harness.permit_digest(PROBE_ORDER, OIF_INPUT_SETTLER, PERMIT2_MAINNET_DOMAIN)
    ) != bytes(digest)
    assert bytes(harness.permit_digest(PROBE_ORDER, LIFI_INPUT_SETTLER, keccak(b"x"))) != bytes(
        digest
    )


# Encoding round trip


def test_encode_decode_round_trip(harness):
    encoded = bytes(harness.encode(PROBE_ORDER))
    assert encoded == encode([ORDER_ABI], [PROBE_ORDER])
    assert _normalize(harness.decode(encoded)) == (
        *PROBE_ORDER[:6],
        [list(pair) for pair in PROBE_ORDER[6]],
        list(PROBE_ORDER[7]),
    )


def test_decode_rejects_lists_and_fields_above_the_bounds(harness):
    output = PROBE_ORDER[7][0]
    for oversized in (
        (*PROBE_ORDER[:6], PROBE_ORDER[6] * 2, PROBE_ORDER[7]),
        (*PROBE_ORDER[:7], PROBE_ORDER[7] * 2),
        (*PROBE_ORDER[:7], [(*output[:6], b"\x01" * (MAX_CALLBACK_DATA + 1), b"")]),
        (*PROBE_ORDER[:7], [(*output[:7], b"\x01" * (MAX_CONTEXT + 1))]),
    ):
        with boa.reverts():
            harness.decode(encode([ORDER_ABI], [oversized]))


# Identifiers and context


def test_identifier_helpers(harness):
    account = to_checksum_address("0x5555555555555555555555555555555555555555")
    assert bytes(harness.to_identifier(account)) == identifier(account)
    assert harness.from_identifier(int(account, 16)) == account
    assert harness.is_clean(int(account, 16))
    assert harness.is_clean(0)
    assert not harness.is_clean(int(account, 16) | (1 << 160))
    assert not harness.is_clean(1 << 255)


@pytest.mark.parametrize(
    "context, expected",
    [
        (b"", True),
        (b"\x00", True),
        (b"\x00\x00", False),
        (b"\xe0" + b"\x11" * 36, True),
        (b"\xe0" + b"\x11" * 35, False),
        (b"\xe0" + b"\x11" * 37, False),
        (b"\x01" + b"\x00" * 40, False),
        (b"\xe1" + b"\x11" * 36, False),
        (b"\xff", False),
    ],
)
def test_context_shapes(harness, context, expected):
    assert harness.resolves_to_signed_amount(context) == expected


# Properties


address_st = st.integers(0, MASK160).map(lambda v: to_checksum_address(v.to_bytes(20, "big")))
bytes32_st = st.binary(min_size=32, max_size=32)
uint_st = st.integers(0, MAX_UINT256)
uint32_st = st.integers(0, 2**32 - 1)
output_st = st.tuples(
    bytes32_st,
    bytes32_st,
    uint_st,
    bytes32_st,
    uint_st,
    bytes32_st,
    st.binary(max_size=MAX_CALLBACK_DATA),
    st.binary(max_size=MAX_CONTEXT),
)
order_st = st.tuples(
    address_st,
    uint_st,
    uint_st,
    uint32_st,
    uint32_st,
    address_st,
    st.lists(st.tuples(st.integers(0, MASK160), uint_st), min_size=0, max_size=MAX_INPUTS),
    st.lists(output_st, min_size=0, max_size=MAX_OUTPUTS),
)


@given(order=order_st, spender=address_st, domain=bytes32_st)
@settings(max_examples=60, deadline=None)
def test_digest_matches_reference(harness, order, spender, domain):
    order = (*order[:6], [list(pair) for pair in order[6]], order[7])
    assert bytes(harness.permit_digest(order, spender, domain)) == permit_digest_reference(
        order, spender, domain
    )
    assert _normalize(harness.decode(bytes(harness.encode(order)))) == (*order[:7], list(order[7]))
