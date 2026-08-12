# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OrderValidatorMock
@author Curve Finance
@license MIT
@notice Configurable validator adapter test double for the ERC-1271 dispatcher:
        replays a settable NormalizedOrder or arbitrary raw return data, and can
        revert on demand.
@dev validate() is @raw_return so tests control the exact returndata bytes the
     dispatcher sees: short, oversized, or dirty-word responses exercise the
     non-reverting raw_call and _decode_normalized_order hardening paths.
     echo_digest patches the recomputed_digest word with the supplied digest so
     happy-path tests do not need to precompute it.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately unsafe and caller-trusting by design.
"""

from contracts.burners.auction import adapter_types


# abi_encode(NormalizedOrder) is a static 12-word tuple; headroom on top lets
# tests return oversized responses past the dispatcher's outsize.
NORMALIZED_ORDER_LEN: constant(uint256) = 12 * 32
MAX_RESPONSE_LEN: constant(uint256) = NORMALIZED_ORDER_LEN + 128

should_revert: public(bool)
echo_digest: public(bool)
raw_response: public(Bytes[MAX_RESPONSE_LEN])


@external
def set_order(_order: adapter_types.NormalizedOrder):
    """@notice Respond with a well-formed encoding of the given order."""
    self.raw_response = abi_encode(_order)


@external
def set_raw_response(_response: Bytes[MAX_RESPONSE_LEN]):
    """@notice Respond with arbitrary raw bytes (short, oversized, or dirty)."""
    self.raw_response = _response


@external
def set_revert(_should_revert: bool):
    self.should_revert = _should_revert


@external
def set_echo_digest(_echo_digest: bool):
    """@notice Overwrite the response's recomputed_digest word with the call's digest."""
    self.echo_digest = _echo_digest


@external
@view
@raw_return
def validate(
    _auction: address,
    _digest: bytes32,
    _payload: Bytes[adapter_types.MAX_ADAPTER_PAYLOAD],
) -> Bytes[MAX_RESPONSE_LEN]:
    assert not self.should_revert, "Validator revert"
    response: Bytes[MAX_RESPONSE_LEN] = self.raw_response
    if self.echo_digest and len(response) == NORMALIZED_ORDER_LEN:
        response = concat(_digest, slice(response, 32, NORMALIZED_ORDER_LEN - 32))
    return response
