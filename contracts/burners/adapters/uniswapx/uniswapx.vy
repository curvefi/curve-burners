# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title UniswapX ExclusiveDutchOrder library
@author Curve Finance
@license MIT
@notice Stateless UniswapX v1 order type (ExclusiveDutchOrder), its EIP-712
        hash, the Permit2 PermitWitnessTransferFrom digest the reactor asks
        the swapper to sign, the reactor's linear decay, and the
        OrderNotValid revert shared by UniswapX-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to UniswapX's
     ExclusiveDutchOrderLib/OrderInfoLib/DutchOrderLib and Permit2's
     PermitHash; the decay to DutchDecayLib.
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape CoW-facing adapters use) so every adapter reports shape and constant
# failures the same way.
error OrderNotValid:
    reason: String[32]


# UniswapX ReactorStructs.OrderInfo: the generic order header every reactor
# order type starts with.
struct OrderInfo:
    reactor: address
    swapper: address
    nonce: uint256
    deadline: uint256
    additionalValidationContract: address
    additionalValidationData: Bytes[MAX_VALIDATION_DATA]


# DutchOrderLib.DutchInput: what the swapper sells, rising linearly from
# startAmount to endAmount.
struct DutchInput:
    token: address
    startAmount: uint256
    endAmount: uint256


# DutchOrderLib.DutchOutput: what the filler pays, falling linearly from
# startAmount to endAmount.
struct DutchOutput:
    token: address
    startAmount: uint256
    endAmount: uint256
    recipient: address


# ExclusiveDutchOrderLib.ExclusiveDutchOrder, the order type of the v1
# ExclusiveDutchOrderReactor.
struct ExclusiveDutchOrder:
    info: OrderInfo
    decayStartTime: uint256
    decayEndTime: uint256
    exclusiveFiller: address
    exclusivityOverrideBps: uint256
    input: DutchInput
    outputs: DynArray[DutchOutput, MAX_OUTPUTS]


# Decoding bounds: the auction sells one lot token for one payment, so an
# order carries exactly one output; longer output lists and longer validation
# payloads fail ABI decoding before any check runs.
MAX_OUTPUTS: constant(uint256) = 1
MAX_VALIDATION_DATA: constant(uint256) = 256
# abi_encode(ExclusiveDutchOrder) at its bounds: outer offset word, nine head
# words (info offset, four scalars, the inline three-word input, outputs
# offset), the info body (six head words, the validation data length word
# and its padded bytes), and the outputs body (length word plus four words
# per output).
ENCODED_ORDER_BOUND: constant(uint256) = (
    32 + 9 * 32 + (6 * 32 + 32 + MAX_VALIDATION_DATA) + (32 + MAX_OUTPUTS * 4 * 32)
)

# EIP-712 type hashes (UniswapX OrderInfoLib, DutchOrderLib,
# ExclusiveDutchOrderLib). Every type string is spelled out in full because
# the hash of a concatenation is not a compile-time constant.
ORDER_INFO_TYPE_HASH: constant(bytes32) = keccak256(
    "OrderInfo(address reactor,address swapper,uint256 nonce,uint256 deadline,"
    "address additionalValidationContract,bytes additionalValidationData)"
)
DUTCH_OUTPUT_TYPE_HASH: constant(bytes32) = keccak256(
    "DutchOutput(address token,uint256 startAmount,uint256 endAmount,address recipient)"
)
# ExclusiveDutchOrderLib.ORDER_TYPE_HASH: the primary type followed by its
# sub-types in the order the library packs them (DutchOutput, OrderInfo).
ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "ExclusiveDutchOrder(OrderInfo info,uint256 decayStartTime,uint256 decayEndTime,"
    "address exclusiveFiller,uint256 exclusivityOverrideBps,address inputToken,"
    "uint256 inputStartAmount,uint256 inputEndAmount,DutchOutput[] outputs)"
    "DutchOutput(address token,uint256 startAmount,uint256 endAmount,address recipient)"
    "OrderInfo(address reactor,address swapper,uint256 nonce,uint256 deadline,"
    "address additionalValidationContract,bytes additionalValidationData)"
)
# Permit2 PermitHash: the witness permit type hash is
# keccak256(_PERMIT_TRANSFER_FROM_WITNESS_TYPEHASH_STUB ++ witnessTypeString)
# with ExclusiveDutchOrderLib.PERMIT2_ORDER_TYPE as the witness type string
# (sub-types in alphabetical order there, as EIP-712 requires).
TOKEN_PERMISSIONS_TYPE_HASH: constant(bytes32) = keccak256(
    "TokenPermissions(address token,uint256 amount)"
)
PERMIT_WITNESS_TYPE_HASH: constant(bytes32) = keccak256(
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
    "uint256 deadline,ExclusiveDutchOrder witness)"
    "DutchOutput(address token,uint256 startAmount,uint256 endAmount,address recipient)"
    "ExclusiveDutchOrder(OrderInfo info,uint256 decayStartTime,uint256 decayEndTime,"
    "address exclusiveFiller,uint256 exclusivityOverrideBps,address inputToken,"
    "uint256 inputStartAmount,uint256 inputEndAmount,DutchOutput[] outputs)"
    "OrderInfo(address reactor,address swapper,uint256 nonce,uint256 deadline,"
    "address additionalValidationContract,bytes additionalValidationData)"
    "TokenPermissions(address token,uint256 amount)"
)  # 0x2846b6ca8e0ecdbc9ca7696f16bdf77b3baf48504ac14d6a541484ec197e91eb


# Hashing


@internal
@pure
def _outputs_hash(_outputs: DynArray[DutchOutput, MAX_OUTPUTS]) -> bytes32:
    # DutchOrderLib.hash(DutchOutput[]): keccak of the packed output hashes
    # (a static bytes32 array abi-encodes to exactly its packed words).
    if len(_outputs) == 0:
        return keccak256(b"")
    hashes: bytes32[MAX_OUTPUTS] = empty(bytes32[MAX_OUTPUTS])
    for i: uint256 in range(len(_outputs), bound=MAX_OUTPUTS):
        hashes[i] = keccak256(abi_encode(DUTCH_OUTPUT_TYPE_HASH, _outputs[i]))
    return keccak256(slice(abi_encode(hashes), 0, len(_outputs) * 32))


@internal
@pure
def _order_hash(_order: ExclusiveDutchOrder) -> bytes32:
    """
    @notice ExclusiveDutchOrderLib.hash: the witness the reactor passes to
            Permit2.
    """
    info_hash: bytes32 = keccak256(
        abi_encode(
            ORDER_INFO_TYPE_HASH,
            _order.info.reactor,
            _order.info.swapper,
            _order.info.nonce,
            _order.info.deadline,
            _order.info.additionalValidationContract,
            keccak256(_order.info.additionalValidationData),
        )
    )
    return keccak256(
        abi_encode(
            ORDER_TYPE_HASH,
            info_hash,
            _order.decayStartTime,
            _order.decayEndTime,
            _order.exclusiveFiller,
            _order.exclusivityOverrideBps,
            _order.input.token,
            _order.input.startAmount,
            _order.input.endAmount,
            self._outputs_hash(_order.outputs),
        )
    )


@internal
@pure
def _permit_digest(_order: ExclusiveDutchOrder, _domain_separator: bytes32) -> bytes32:
    """
    @notice The EIP-712 digest Permit2 verifies for the reactor's
            permitWitnessTransferFrom: the permit is (input token, input
            endAmount, nonce, deadline) with the reactor as spender and the
            order hash as witness (ExclusiveDutchOrderReactor via Permit2Lib).
    @param _order The order being settled.
    @param _domain_separator Permit2's EIP-712 domain separator.
    @return The digest passed to the swapper's isValidSignature.
    """
    permitted_hash: bytes32 = keccak256(
        abi_encode(TOKEN_PERMISSIONS_TYPE_HASH, _order.input.token, _order.input.endAmount)
    )
    struct_hash: bytes32 = keccak256(
        abi_encode(
            PERMIT_WITNESS_TYPE_HASH,
            permitted_hash,
            _order.info.reactor,
            _order.info.nonce,
            _order.info.deadline,
            self._order_hash(_order),
        )
    )
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


# Decay


@internal
@view
def _decayed_amount(
    _start_amount: uint256, _end_amount: uint256, _decay_start: uint256, _decay_end: uint256
) -> uint256:
    """
    @notice DutchDecayLib.decay at the current block: the amount the reactor
            resolves for a linearly decaying input or output.
    @dev Reverts like the reactor (EndTimeBeforeStartTime) when the decay
         window is inverted for an actually decaying amount, so a
         reactor-rejected order is never reported as valid on the amount
         branch. Rounds toward the start amount like mulDivDown.
    """
    if _start_amount == _end_amount:
        return _start_amount
    assert _decay_start < _decay_end, OrderNotValid(reason="BadDecayTimes")
    if _decay_end <= block.timestamp:
        return _end_amount
    if _decay_start >= block.timestamp:
        return _start_amount
    elapsed: uint256 = block.timestamp - _decay_start
    duration: uint256 = _decay_end - _decay_start
    if _end_amount < _start_amount:
        return _start_amount - (_start_amount - _end_amount) * elapsed // duration
    return _start_amount + (_end_amount - _start_amount) * elapsed // duration


@internal
@view
def _decayed_output(_output: DutchOutput, _decay_start: uint256, _decay_end: uint256) -> uint256:
    """
    @notice DutchDecayLib.decay(DutchOutput): the output amount the reactor
            requires from the filler now. Outputs only ever decay downward;
            the reactor rejects the opposite (IncorrectAmounts) and so does
            this.
    """
    assert _output.startAmount >= _output.endAmount, OrderNotValid(reason="BadDecayDirection")
    return self._decayed_amount(_output.startAmount, _output.endAmount, _decay_start, _decay_end)
