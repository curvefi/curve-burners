# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title CoW GPv2 order library
@author Curve Finance
@license MIT
@notice Stateless GPv2 order type, EIP-712 digest, and flag checks plus the
        CoW-canonical OrderNotValid revert.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. The digest computation must stay byte-identical to the GPv2
     EIP-712 reference.
"""


# CoW-canonical revert ABI (cowprotocol IConditionalOrder): the selector must
# stay exactly OrderNotValid(string) — CoW tooling classifies ERC-1271 reverts
# by it.
error OrderNotValid:
    reason: String[32]


struct GPv2Order:
    sellToken: address
    buyToken: address
    receiver: address
    sellAmount: uint256
    buyAmount: uint256
    validTo: uint32
    appData: bytes32
    feeAmount: uint256
    kind: bytes32
    partiallyFillable: bool
    sellTokenBalance: bytes32
    buyTokenBalance: bytes32


ENCODED_ORDER_LEN: constant(uint256) = 12 * 32

# GPv2 constants from cowprotocol/contracts@a10f40788af29467e87de3dbf2196662b0a6b500 GPv2Order.
GPV2_ORDER_TYPE_HASH: constant(bytes32) = 0xd5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489
SELL_KIND: public(constant(bytes32)) = 0xf3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775
TOKEN_BALANCE: public(constant(bytes32)) = 0x5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9
ERC1271_MAGIC_VALUE: public(constant(bytes4)) = 0x1626ba7e


@internal
@pure
def _order_digest(_order: GPv2Order, _domain_separator: bytes32) -> bytes32:
    struct_hash: bytes32 = keccak256(
        abi_encode(
            GPV2_ORDER_TYPE_HASH,
            _order.sellToken,
            _order.buyToken,
            _order.receiver,
            _order.sellAmount,
            _order.buyAmount,
            _order.validTo,
            _order.appData,
            _order.feeAmount,
            _order.kind,
            _order.partiallyFillable,
            _order.sellTokenBalance,
            _order.buyTokenBalance,
        )
    )
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


# The flag checks are split so callers keep the established CoW-canonical error
# granularity: BadOrderFlags for fee/kind/partial and BadBalanceMode for
# balance modes. Both are boolean by design: revert-versus-invalid policy
# stays with the caller.
@internal
@pure
def _check_order_flags(_order: GPv2Order) -> bool:
    """@notice Return whether an order is a zero-fee partially fillable sell order."""
    return _order.feeAmount == 0 and _order.kind == SELL_KIND and _order.partiallyFillable


@internal
@pure
def _check_balance_modes(_order: GPv2Order) -> bool:
    """@notice Return whether both balance modes are plain ERC-20."""
    return _order.sellTokenBalance == TOKEN_BALANCE and _order.buyTokenBalance == TOKEN_BALANCE
