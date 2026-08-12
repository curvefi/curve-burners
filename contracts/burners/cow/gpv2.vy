# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CoW GPv2 order library
@author Curve Finance
@license MIT
@notice Stateless GPv2 order construction, hashing, flag, bucket, and
        conditional-order codec helpers plus the watchtower revert ABI.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Shared by the embedded execution rail, the standalone watchtower
     handler, and CoW order validators; the digest computation must stay
     byte-identical to the GPv2 EIP-712 reference.
"""


# Watchtower-canonical revert ABI (cowprotocol IConditionalOrder and the
# watchtower's custom-error polling protocol). Selectors must stay exactly
# OrderNotValid(string) and PollTryAtEpoch(uint256,string): the off-chain
# watchtower classifies handler reverts by these signatures.
error OrderNotValid:
    reason: String[32]


error PollTryAtEpoch:
    timestamp: uint256
    reason: String[32]


# Auction-specific: CoW rail switched off.
error CowDisabled:
    pass


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


struct ConditionalOrderParams:
    handler: address
    salt: bytes32
    staticData: Bytes[STATIC_INPUT_LEN]


struct PayloadStruct:
    proof: DynArray[bytes32, MAX_PROOF_LEN]
    params: ConditionalOrderParams
    offchainInput: Bytes[MAX_OFFCHAIN_INPUT_LEN]


# Conditional-order encoding limits
STATIC_INPUT_LEN: constant(uint256) = 52  # packed bytes20 token || bytes32 generation
MAX_HANDLER_INPUT_LEN: constant(uint256) = 256
MAX_OFFCHAIN_INPUT_LEN: constant(uint256) = 256
MAX_PROOF_LEN: constant(uint256) = 32
ENCODED_ORDER_LEN: constant(uint256) = 12 * 32

# GPv2 constants from cowprotocol/contracts@a10f40788af29467e87de3dbf2196662b0a6b500 GPv2Order.
GPV2_ORDER_TYPE_HASH: constant(bytes32) = 0xd5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489
SELL_KIND: public(constant(bytes32)) = 0xf3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775
TOKEN_BALANCE: public(constant(bytes32)) = 0x5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9
ERC1271_MAGIC_VALUE: public(constant(bytes4)) = 0x1626ba7e
CONDITIONAL_ORDER_GENERATOR_INTERFACE: public(constant(bytes4)) = 0xb8296fc4
SIGNATURE_VERIFIER_MUXER_INTERFACE: public(constant(bytes4)) = 0x62af8dc2


# Watchtower error helpers so the ComposableCoW revert ABI is encoded in
# exactly one place.
@internal
@pure
def _order_not_valid(_reason: String[32]):
    raise OrderNotValid(reason=_reason)


@internal
@pure
def _poll_try_at(_timestamp: uint256, _reason: String[32]):
    raise PollTryAtEpoch(timestamp=_timestamp, reason=_reason)


@internal
@pure
def _encode_static_input(_token: address, _generation: uint256) -> Bytes[STATIC_INPUT_LEN]:
    return concat(convert(_token, bytes20), convert(_generation, bytes32))


@internal
@pure
def _decode_static_input(
    _static_input: Bytes[MAX_HANDLER_INPUT_LEN],
) -> (bool, address, uint256):
    """
    @notice Decode packed conditional-order static input without reverting.
    @dev Callers keep their own revert-versus-invalid policy; registration
         bookkeeping belongs to the watchtower publishing shim.
    @return ok, token, generation
    """
    if len(_static_input) != STATIC_INPUT_LEN:
        return False, empty(address), 0

    token: address = convert(convert(slice(_static_input, 0, 20), bytes20), address)
    generation: uint256 = extract32(_static_input, 20, output_type=uint256)
    if token == empty(address) or self._encode_static_input(token, generation) != _static_input:
        return False, empty(address), 0
    return True, token, generation


@internal
@pure
def _bucket_quote_time(_timestamp: uint256, _start: uint256, _validity: uint256) -> uint256:
    bucket_start: uint256 = _timestamp // _validity * _validity
    return max(bucket_start, _start)


@internal
@pure
def _bucket_valid_to(_timestamp: uint256, _end: uint256, _validity: uint256) -> uint32:
    bucket_end: uint256 = (_timestamp // _validity + 1) * _validity
    return convert(min(bucket_end, _end), uint32)


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


@internal
@pure
def _build_sell_order(
    _sell_token: address,
    _buy_token: address,
    _receiver: address,
    _sell_amount: uint256,
    _buy_amount: uint256,
    _valid_to: uint32,
    _app_data: bytes32,
) -> GPv2Order:
    """@notice Build the canonical partially fillable ERC-20 sell order."""
    return GPv2Order(
        sellToken=_sell_token,
        buyToken=_buy_token,
        receiver=_receiver,
        sellAmount=_sell_amount,
        buyAmount=_buy_amount,
        validTo=_valid_to,
        appData=_app_data,
        feeAmount=0,
        kind=SELL_KIND,
        partiallyFillable=True,
        sellTokenBalance=TOKEN_BALANCE,
        buyTokenBalance=TOKEN_BALANCE,
    )


# The flag checks are split so callers keep the established watchtower error
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
