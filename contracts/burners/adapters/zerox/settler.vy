# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title 0x Settler gasless order library
@author Curve Finance
@license MIT
@notice Stateless 0x Settler v2 gasless-flow types (the Permit2 permit, the
        AllowedSlippage every Settler flavor signs over, the order an adapter
        validates), the two Permit2 witness shapes (SettlerMetaTxn's
        SlippageAndActions, SettlerIntent's Slippage), the Settler's hash of
        its action list, the Permit2 PermitWitnessTransferFrom digest the
        Settler asks the taker to sign, and the OrderNotValid revert shared
        by 0x-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to 0x-settler (SettlerAbstract
     SLIPPAGE_AND_ACTIONS_TYPE / SLIPPAGE_TYPE, SettlerMetaTxn
     _hashArrayOfBytes / _hashActionsAndSlippage, SettlerIntent
     _hashSlippage, Permit2PaymentMetaTxn / Permit2PaymentIntent
     _witnessTypeSuffix) and to Permit2's PermitHash.hashWithWitness.
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape every other adapter uses) so shape and constant failures are
# reported the same way.
error OrderNotValid:
    reason: String[32]


# ISignatureTransfer.PermitTransferFrom flattened: what Permit2 pulls
# (TokenPermissions) under which unordered nonce and until when. The spender
# is the Settler and is not part of the struct (Permit2 reads msg.sender).
struct Permit:
    token: address
    amount: uint256
    nonce: uint256
    deadline: uint256


# ISettlerBase.AllowedSlippage: the floor the Settler enforces after the
# action list ran (_checkSlippageAndTransfer): at least minAmountOut of
# buyToken from its own balance to recipient.
struct Slippage:
    recipient: address
    buyToken: address
    minAmountOut: uint256


# The gasless order an adapter validates: the permit, the slippage, and the
# Settler's hash of the signed action list (SettlerMetaTxn only; zero for
# SettlerIntent, whose actions are the solver's and unsigned). The actions
# themselves are not carried: the digest binds them through this hash and
# none of them can lower the floor, so an adapter has nothing to read in
# them.
struct Order:
    permit: Permit
    slippage: Slippage
    actions_hash: bytes32


# abi_encode(Order): three static structs, eight words.
ENCODED_ORDER_LEN: constant(uint256) = 8 * 32

# Bounds of the action list the hashing helper accepts (eth_call use by
# publishers only; the adapter never materializes actions). Sized above the
# largest fills seen in Settler traffic (40 actions, 7.9 KB single action).
MAX_ACTIONS: constant(uint256) = 64
MAX_ACTION_LEN: constant(uint256) = 8192

# EIP-712 type hashes. SettlerAbstract.SLIPPAGE_AND_ACTIONS_TYPEHASH and
# SLIPPAGE_TYPEHASH; the Permit2 witness permit type hashes are Permit2's
# _PERMIT_TRANSFER_FROM_WITNESS_TYPEHASH_STUB followed by the Settler
# flavor's _witnessTypeSuffix (sub-types in alphabetical order there, as
# EIP-712 requires).
SLIPPAGE_AND_ACTIONS_TYPE_HASH: constant(bytes32) = keccak256(
    "SlippageAndActions(address recipient,address buyToken,uint256 minAmountOut,bytes[] actions)"
)  # 0x615e8d716cef7295e75dd3f1f10d679914ad6d7759e8e9459f0109ef75241701
SLIPPAGE_TYPE_HASH: constant(bytes32) = keccak256(
    "Slippage(address recipient,address buyToken,uint256 minAmountOut)"
)  # 0xdc83993a2ffc65b01b71ed08790b6e39c5c55d76937b62a3b5085b02071f1259
TOKEN_PERMISSIONS_TYPE_HASH: constant(bytes32) = keccak256(
    "TokenPermissions(address token,uint256 amount)"
)
PERMIT_WITNESS_METATXN_TYPE_HASH: constant(bytes32) = keccak256(
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
    "uint256 deadline,SlippageAndActions slippageAndActions)"
    "SlippageAndActions(address recipient,address buyToken,uint256 minAmountOut,bytes[] actions)"
    "TokenPermissions(address token,uint256 amount)"
)  # 0xe363956cfcb5235635a38cff995a4ba597e7bf5a7055f871efd13bf94cbbd8a3
PERMIT_WITNESS_INTENT_TYPE_HASH: constant(bytes32) = keccak256(
    "PermitWitnessTransferFrom(TokenPermissions permitted,address spender,uint256 nonce,"
    "uint256 deadline,Slippage slippage)"
    "Slippage(address recipient,address buyToken,uint256 minAmountOut)"
    "TokenPermissions(address token,uint256 amount)"
)  # 0x87a28f49641438268f602aad55df170303c01a470429159023f1af549bb2c2ba


# Hashing


@internal
@pure
def _hash_actions(_actions: DynArray[Bytes[MAX_ACTION_LEN], MAX_ACTIONS]) -> bytes32:
    """
    @notice SettlerMetaTxn._hashArrayOfBytes: keccak of the packed keccaks
            of every action.
    @dev The Settler rejects actions shorter than a selector; here they
         hash like any other bytes, so a digest over such a list still
         matches and the Settler is what fails the fill.
    """
    if len(_actions) == 0:
        return keccak256(b"")
    hashes: bytes32[MAX_ACTIONS] = empty(bytes32[MAX_ACTIONS])
    for i: uint256 in range(len(_actions), bound=MAX_ACTIONS):
        hashes[i] = keccak256(_actions[i])
    return keccak256(slice(abi_encode(hashes), 0, len(_actions) * 32))


@internal
@pure
def _witness_hash(_order: Order, _intent: bool) -> bytes32:
    """
    @notice The witness the Settler passes to Permit2: SettlerIntent's
            _hashSlippage (Slippage) or SettlerMetaTxn's
            _hashActionsAndSlippage (SlippageAndActions).
    """
    if _intent:
        return keccak256(abi_encode(SLIPPAGE_TYPE_HASH, _order.slippage))
    return keccak256(
        abi_encode(SLIPPAGE_AND_ACTIONS_TYPE_HASH, _order.slippage, _order.actions_hash)
    )


@internal
@pure
def _permit_digest(
    _order: Order, _spender: address, _domain_separator: bytes32, _intent: bool
) -> bytes32:
    """
    @notice The EIP-712 digest Permit2 verifies for the Settler's
            permitWitnessTransferFrom (Permit2PaymentMetaTxn._transferFrom):
            the permit with the Settler as spender and the flavor's witness.
    @param _order The order being settled.
    @param _spender The Settler calling Permit2.
    @param _domain_separator Permit2's EIP-712 domain separator.
    @param _intent True for SettlerIntent (Slippage witness), False for
           SettlerMetaTxn (SlippageAndActions witness).
    @return The digest passed to the taker's isValidSignature.
    """
    permitted_hash: bytes32 = keccak256(
        abi_encode(TOKEN_PERMISSIONS_TYPE_HASH, _order.permit.token, _order.permit.amount)
    )
    type_hash: bytes32 = PERMIT_WITNESS_METATXN_TYPE_HASH
    if _intent:
        type_hash = PERMIT_WITNESS_INTENT_TYPE_HASH
    struct_hash: bytes32 = keccak256(
        abi_encode(
            type_hash,
            permitted_hash,
            _spender,
            _order.permit.nonce,
            _order.permit.deadline,
            self._witness_hash(_order, _intent),
        )
    )
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))
