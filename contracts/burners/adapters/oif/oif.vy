# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Open Intents Framework escrow order library
@author Curve Finance
@license MIT
@notice Stateless OIF StandardOrder and MandateOutput types, the Permit2
        witness the escrow input settler asks the sponsor to sign
        (PermitBatchWitnessTransferFrom with Permit2Witness), the output
        settler's context shapes, and the OrderNotValid revert shared by
        OIF-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to openintentsframework
     oif-contracts (MandateOutputType, Permit2WitnessType, InputSettlerEscrow
     and the LI.FI deployment InputSettlerEscrowLIFI) and to Permit2's
     PermitHash; the context layout to OutputSettlerSimple / FulfilmentLib.
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape every other adapter uses) so shape and constant failures are
# reported the same way.
error OrderNotValid:
    reason: String[32]


# MandateOutputType.MandateOutput: what the solver must deliver. Addresses
# are bytes32 identifiers (the address in the low 160 bits).
struct MandateOutput:
    oracle: bytes32
    settler: bytes32
    chainId: uint256
    token: bytes32
    amount: uint256
    recipient: bytes32
    callbackData: Bytes[MAX_CALLBACK_DATA]
    context: Bytes[MAX_CONTEXT]


# StandardOrderType.StandardOrder: the intent the escrow opens. Inputs are
# (token identifier, amount) pairs pulled from the user.
struct StandardOrder:
    user: address
    nonce: uint256
    originChainId: uint256
    expires: uint32
    fillDeadline: uint32
    inputOracle: address
    inputs: DynArray[uint256[2], MAX_INPUTS]
    outputs: DynArray[MandateOutput, MAX_OUTPUTS]


# Decoding bounds: the auction sells one lot token for one payment, so an
# order carries exactly one input and one output; longer lists fail ABI
# decoding before any check runs. The output's callbackData is never used
# (the recipient is the FeeCollector) and its context is at most the
# exclusive limit order's 37 bytes.
MAX_INPUTS: constant(uint256) = 1
MAX_OUTPUTS: constant(uint256) = 1
MAX_CALLBACK_DATA: constant(uint256) = 64
MAX_CONTEXT: constant(uint256) = 64
# abi_encode(StandardOrder) at the bounds: the tuple offset word, eight head
# words, the inputs body (length word plus two words per input), and the
# outputs body (length word, one offset word per output, and per output
# eight head words plus the two dynamic bodies).
ENCODED_ORDER_BOUND: constant(uint256) = (
    32
    + 8 * 32
    + (32 + MAX_INPUTS * 2 * 32)
    + (32 + MAX_OUTPUTS * (32 + 8 * 32 + (32 + MAX_CALLBACK_DATA) + (32 + MAX_CONTEXT)))
)

# Identifiers: an address occupies the low 160 bits of a bytes32; the
# settlers reject anything else (validatedCleanAddress).
ADDRESS_MASK: constant(uint256) = 2**160 - 1

# OutputSettlerSimple / FulfilmentLib order types in `context`: an empty
# context or a single LIMIT_ORDER byte resolves to the signed amount; an
# EXCLUSIVE_LIMIT_ORDER (37 bytes: type, solver identifier, start time)
# also resolves to the signed amount but only for one solver until the
# start; DUTCH_AUCTION resolves to a time-dependent amount instead.
LIMIT_ORDER: constant(bytes1) = 0x00
DUTCH_AUCTION: constant(bytes1) = 0x01
EXCLUSIVE_LIMIT_ORDER: constant(bytes1) = 0xE0
EXCLUSIVE_LIMIT_ORDER_LEN: constant(uint256) = 37

# EIP-712 type hashes. MandateOutputType.MANDATE_OUTPUT_TYPE_HASH and
# Permit2WitnessType.PERMIT2_WITNESS_TYPE_HASH (the primary type followed by
# its sub-type); the Permit2 batch witness type hash is Permit2's
# _PERMIT_BATCH_WITNESS_TRANSFER_FROM_TYPEHASH_STUB followed by
# Permit2WitnessType.PERMIT2_PERMIT2_TYPESTRING (sub-types in alphabetical
# order there, as EIP-712 requires).
MANDATE_OUTPUT_TYPE_HASH: constant(bytes32) = keccak256(
    "MandateOutput(bytes32 oracle,bytes32 settler,uint256 chainId,bytes32 token,"
    "uint256 amount,bytes32 recipient,bytes callbackData,bytes context)"
)
PERMIT2_WITNESS_TYPE_HASH: constant(bytes32) = keccak256(
    "Permit2Witness(address user,uint32 expires,address inputOracle,MandateOutput[] outputs)"
    "MandateOutput(bytes32 oracle,bytes32 settler,uint256 chainId,bytes32 token,"
    "uint256 amount,bytes32 recipient,bytes callbackData,bytes context)"
)
TOKEN_PERMISSIONS_TYPE_HASH: constant(bytes32) = keccak256(
    "TokenPermissions(address token,uint256 amount)"
)
PERMIT_BATCH_WITNESS_TYPE_HASH: constant(bytes32) = keccak256(
    "PermitBatchWitnessTransferFrom(TokenPermissions[] permitted,address spender,"
    "uint256 nonce,uint256 deadline,Permit2Witness witness)"
    "MandateOutput(bytes32 oracle,bytes32 settler,uint256 chainId,bytes32 token,"
    "uint256 amount,bytes32 recipient,bytes callbackData,bytes context)"
    "Permit2Witness(address user,uint32 expires,address inputOracle,MandateOutput[] outputs)"
    "TokenPermissions(address token,uint256 amount)"
)


# Identifiers


@internal
@pure
def _to_identifier(_account: address) -> bytes32:
    return convert(convert(_account, uint256), bytes32)


@internal
@pure
def _is_clean(_identifier: uint256) -> bool:
    """
    @notice Whether an identifier carries nothing above its address bits
            (LibAddress.validatedCleanAddress accepts it).
    """
    return _identifier & ~ADDRESS_MASK == 0


@internal
@pure
def _from_identifier(_identifier: uint256) -> address:
    return convert(convert(_identifier & ADDRESS_MASK, uint160), address)


# Hashing


@internal
@pure
def _output_hash(_output: MandateOutput) -> bytes32:
    """
    @notice MandateOutputType.hashOutput.
    """
    return keccak256(
        abi_encode(
            MANDATE_OUTPUT_TYPE_HASH,
            _output.oracle,
            _output.settler,
            _output.chainId,
            _output.token,
            _output.amount,
            _output.recipient,
            keccak256(_output.callbackData),
            keccak256(_output.context),
        )
    )


@internal
@pure
def _outputs_hash(_outputs: DynArray[MandateOutput, MAX_OUTPUTS]) -> bytes32:
    """
    @notice MandateOutputType.hashOutputs: keccak of the packed output hashes.
    """
    if len(_outputs) == 0:
        return keccak256(b"")
    hashes: bytes32[MAX_OUTPUTS] = empty(bytes32[MAX_OUTPUTS])
    for i: uint256 in range(len(_outputs), bound=MAX_OUTPUTS):
        hashes[i] = self._output_hash(_outputs[i])
    return keccak256(slice(abi_encode(hashes), 0, len(_outputs) * 32))


@internal
@pure
def _witness_hash(_order: StandardOrder) -> bytes32:
    """
    @notice Permit2WitnessType.Permit2WitnessHash: the witness the escrow
            passes to Permit2.
    """
    return keccak256(
        abi_encode(
            PERMIT2_WITNESS_TYPE_HASH,
            _order.user,
            _order.expires,
            _order.inputOracle,
            self._outputs_hash(_order.outputs),
        )
    )


@internal
@pure
def _permitted_hash(_inputs: DynArray[uint256[2], MAX_INPUTS]) -> bytes32:
    """
    @notice keccak of the packed TokenPermissions hashes of the order inputs
            (PermitHash.hashWithWitness for a batch).
    """
    if len(_inputs) == 0:
        return keccak256(b"")
    hashes: bytes32[MAX_INPUTS] = empty(bytes32[MAX_INPUTS])
    for i: uint256 in range(len(_inputs), bound=MAX_INPUTS):
        hashes[i] = keccak256(
            abi_encode(
                TOKEN_PERMISSIONS_TYPE_HASH,
                self._from_identifier(_inputs[i][0]),
                _inputs[i][1],
            )
        )
    return keccak256(slice(abi_encode(hashes), 0, len(_inputs) * 32))


@internal
@pure
def _permit_digest(_order: StandardOrder, _spender: address, _domain_separator: bytes32) -> bytes32:
    """
    @notice The EIP-712 digest Permit2 verifies for the escrow's
            permitWitnessTransferFrom: the batch permit is (input tokens and
            amounts, order.nonce, order.fillDeadline) with the escrow as
            spender and the Permit2Witness hash as witness
            (InputSettlerEscrow._openForWithPermit2).
    @param _order The order being opened.
    @param _spender The escrow input settler calling Permit2.
    @param _domain_separator Permit2's EIP-712 domain separator.
    @return The digest passed to the sponsor's isValidSignature.
    """
    struct_hash: bytes32 = keccak256(
        abi_encode(
            PERMIT_BATCH_WITNESS_TYPE_HASH,
            self._permitted_hash(_order.inputs),
            _spender,
            _order.nonce,
            convert(_order.fillDeadline, uint256),
            self._witness_hash(_order),
        )
    )
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


# Context


@internal
@pure
def _resolves_to_signed_amount(_context: Bytes[MAX_CONTEXT]) -> bool:
    """
    @notice Whether OutputSettlerSimple._resolveOutput pays exactly the
            signed amount for this context: none, a limit order, or an
            exclusive limit order.
    """
    if len(_context) == 0:
        return True
    order_type: bytes1 = convert(slice(_context, 0, 1), bytes1)
    if order_type == LIMIT_ORDER:
        return len(_context) == 1
    if order_type == EXCLUSIVE_LIMIT_ORDER:
        return len(_context) == EXCLUSIVE_LIMIT_ORDER_LEN
    return False
