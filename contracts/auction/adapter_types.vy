# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Adapter types
@author Curve Finance
@license MIT
@notice Shared adapter-registry structs and the versioned ERC-1271 envelope codec.
@dev Stateless module: constants, structs, and pure helpers only. The envelope is
     Curve-specific routing ABI; it is not authorization by itself. All economic
     authority stays with the protocol digest and the auction core checks.
"""


# Shared auction-domain error: raised by the core when staging the want token
# and by the adapters module when syncing its allowances. Declared here because
# error names are globally unique per compilation unit.
error TargetToken:
    pass


# Closed AuthorizationMode enum. The auction resolves approval routers only from
# these modes, so a mutable registry can never introduce an arbitrary spender.
MODE_NONE: constant(uint8) = 0
MODE_COW_VAULT_RELAYER: constant(uint8) = 1
MODE_PERMIT2_SIGNATURE_TRANSFER: constant(uint8) = 2
MODE_PERMIT2_ALLOWANCE_TRANSFER: constant(uint8) = 3


# The registry maps adapter_id (bytes4) to this config. adapter_id is the
# lookup key and is deliberately not duplicated inside the struct (deviation
# from the prompt's Solidity sketch): the dispatcher resolves the config by the
# envelope's adapter_id, so an embedded copy could only disagree with its key.
struct AdapterConfig:
    validator: address
    validator_codehash: bytes32
    verifier: address
    executor: address
    authorization_mode: uint8
    allow_partial_fills: bool
    active: bool
    version: uint16


struct NormalizedOrder:
    recomputed_digest: bytes32
    context_hash: bytes32
    auction_epoch: uint256
    sell_token: address
    buy_token: address
    receiver: address
    verifier: address
    executor: address
    sell_amount: uint256
    min_buy_amount: uint256
    valid_to: uint256
    partially_fillable: bool


# Signature envelope wire format:
#   | magic:4 | version:1 | adapter_id:4 | adapter_version:2 | payload:N |
# ENVELOPE_MAGIC = keccak256("CURVE_DUTCH_AUCTION_ENVELOPE_V1")[:4]. The legacy
# ComposableCoW signature is abi_encode(GPv2Order, PayloadStruct), whose first
# four bytes are the zero padding of the sellToken address head, so a non-zero
# magic prefix cannot collide with it.
ENVELOPE_MAGIC: constant(bytes4) = 0x5a16f8e7
ENVELOPE_VERSION: constant(uint8) = 1
ENVELOPE_HEADER_LEN: constant(uint256) = 11
# Covers the largest supported adapter payload; the legacy ComposableCoW
# signature bound (2048) fits with headroom for future protocol payloads.
MAX_ADAPTER_PAYLOAD: constant(uint256) = 4096
MAX_ENVELOPE_LEN: constant(uint256) = ENVELOPE_HEADER_LEN + MAX_ADAPTER_PAYLOAD


@internal
@pure
def _has_envelope_magic(_signature: Bytes[MAX_ENVELOPE_LEN]) -> bool:
    """
    @notice Return whether bytes claim the adapter-envelope routing path.
    @dev A magic prefix commits the caller to the adapter path: malformed
         envelopes must become invalid signatures, not embedded-path fallbacks.
    """
    if len(_signature) < 4:
        return False
    return convert(slice(_signature, 0, 4), bytes4) == ENVELOPE_MAGIC


@internal
@pure
def _encode_envelope(
    _adapter_id: bytes4,
    _adapter_version: uint16,
    _payload: Bytes[MAX_ADAPTER_PAYLOAD],
) -> Bytes[MAX_ENVELOPE_LEN]:
    """@notice Encode the current-version signature envelope around a payload."""
    return concat(
        ENVELOPE_MAGIC,
        convert(ENVELOPE_VERSION, bytes1),
        _adapter_id,
        convert(_adapter_version, bytes2),
        _payload,
    )


@internal
@pure
def _decode_envelope(
    _signature: Bytes[MAX_ENVELOPE_LEN],
) -> (bool, uint8, bytes4, uint16, Bytes[MAX_ADAPTER_PAYLOAD]):
    """
    @notice Decode an envelope without reverting on malformed input.
    @dev Returns ok=False for short input or a missing magic prefix. Version and
         adapter checks stay with the dispatcher: ok=True only states the wire
         layout parsed, it does not validate anything.
    @return ok, version, adapter_id, adapter_version, payload
    """
    if len(_signature) < ENVELOPE_HEADER_LEN or not self._has_envelope_magic(_signature):
        return False, 0, empty(bytes4), 0, b""

    version: uint8 = convert(convert(slice(_signature, 4, 1), bytes1), uint8)
    adapter_id: bytes4 = convert(slice(_signature, 5, 4), bytes4)
    adapter_version: uint16 = convert(convert(slice(_signature, 9, 2), bytes2), uint16)

    payload: Bytes[MAX_ADAPTER_PAYLOAD] = b""
    if len(_signature) > ENVELOPE_HEADER_LEN:
        # Narrow Bytes[MAX_ENVELOPE_LEN] to Bytes[MAX_ADAPTER_PAYLOAD]; the
        # runtime length check cannot fail because the tail is at most
        # MAX_ENVELOPE_LEN - ENVELOPE_HEADER_LEN = MAX_ADAPTER_PAYLOAD bytes.
        payload = abi_decode(
            abi_encode(
                slice(_signature, ENVELOPE_HEADER_LEN, len(_signature) - ENVELOPE_HEADER_LEN)
            ),
            Bytes[MAX_ADAPTER_PAYLOAD],
        )
    return True, version, adapter_id, adapter_version, payload
