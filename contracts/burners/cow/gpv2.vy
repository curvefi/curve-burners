# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title CoW GPv2 order library
@author Curve Finance
@license MIT
@notice Stateless GPv2 order type, EIP-712 digest, flag checks, and the
        OrderNotValid revert shared by CoW-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. The digest computation must stay byte-identical to the GPv2
     EIP-712 reference.
"""


# Protocol-level order rejection (selector OrderNotValid(string), as in CoW's
# IConditionalOrder) so every CoW-facing adapter reports shape and constant
# failures the same way.
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


# Every field is a static ABI type, so the struct encodes to 12 words.
ENCODED_ORDER_LEN: constant(uint256) = 12 * 32

# GPv2Order constants (cowprotocol/contracts GPv2Order.sol): the EIP-712 type
# hash and the enum-as-hash flag values.
GPV2_ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "Order(address sellToken,address buyToken,address receiver,uint256 sellAmount,"
    "uint256 buyAmount,uint32 validTo,bytes32 appData,uint256 feeAmount,string kind,"
    "bool partiallyFillable,string sellTokenBalance,string buyTokenBalance)"
)  # 0xd5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489
SELL_KIND: constant(bytes32) = keccak256("sell")  # 0xf3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775
TOKEN_BALANCE: constant(bytes32) = keccak256("erc20")  # 0x5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9


@internal
@pure
def _order_digest(_order: GPv2Order, _domain_separator: bytes32) -> bytes32:
    # All order fields are static types, so abi_encode(struct) is exactly the
    # word-per-field layout hashStruct expects.
    struct_hash: bytes32 = keccak256(abi_encode(GPV2_ORDER_TYPE_HASH, _order))
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))
