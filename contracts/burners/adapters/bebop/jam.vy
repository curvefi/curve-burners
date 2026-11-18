# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Bebop JAM order library
@author Curve Finance
@license MIT
@notice Stateless Bebop JAM v2 order type, its EIP-712 digest and domain,
        the protocol's calldata shape, the partnerInfo packing, and the
        OrderNotValid revert shared by JAM-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to JamOrderLib.hash and
     JamValidation (bebop-dex/bebop-jam-contracts, deployed as
     JamSettlement), the partnerInfo layout to JamPartner.unpackPartnerInfo.
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape the CoW- and 1inch-facing adapters use) so every adapter reports
# shape and constant failures the same way.
error OrderNotValid:
    reason: String[32]


# The signed JamOrder: the twelve EIP-712 fields in type order. hooksHash is
# the hash of the taker's pre/post interactions (zero for none) and takes the
# place of the protocol's usingPermit2 flag, which is not signed. This is the
# `toSign` object the JAM API returns, field for field.
struct JamOrder:
    taker: address
    receiver: address
    expiry: uint256
    exclusivityDeadline: uint256
    nonce: uint256
    executor: address
    partnerInfo: uint256
    sellTokens: DynArray[address, MAX_TOKENS]
    buyTokens: DynArray[address, MAX_TOKENS]
    sellAmounts: DynArray[uint256, MAX_TOKENS]
    buyAmounts: DynArray[uint256, MAX_TOKENS]
    hooksHash: bytes32


# The same order as the protocol ABI spells it (JamOrder.sol): usingPermit2
# in place of hooksHash, which the settlement derives from hooksData. Calls
# into the settlement (hashJamOrder, settle, settleInternal) take this shape.
struct JamOrderCalldata:
    taker: address
    receiver: address
    expiry: uint256
    exclusivityDeadline: uint256
    nonce: uint256
    executor: address
    partnerInfo: uint256
    sellTokens: DynArray[address, MAX_TOKENS]
    buyTokens: DynArray[address, MAX_TOKENS]
    sellAmounts: DynArray[uint256, MAX_TOKENS]
    buyAmounts: DynArray[uint256, MAX_TOKENS]
    usingPermit2: bool


# Decoding bound of the token and amount arrays. Adapters sell one lot
# against one payment token, so a payload with more entries than this fails
# ABI decoding before any check runs; the library hashes any order within
# the bound so vectors with several tokens per side can be checked.
MAX_TOKENS: constant(uint256) = 8
# abi_encode(JamOrder) at the bound: the tuple offset word, twelve head
# words, four arrays of a length word and MAX_TOKENS elements each.
ENCODED_ORDER_BOUND: constant(uint256) = 32 + 12 * 32 + 4 * (32 + MAX_TOKENS * 32)
# abi_encode(JamOrder) with one token per side: the shape adapters accept.
ENCODED_SINGLE_ORDER_LEN: constant(uint256) = 32 + 12 * 32 + 4 * 2 * 32

# JamOrderLib.ORDER_TYPE_HASH.
ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "JamOrder(address taker,address receiver,uint256 expiry,uint256 exclusivityDeadline,"
    "uint256 nonce,address executor,uint256 partnerInfo,address[] sellTokens,"
    "address[] buyTokens,uint256[] sellAmounts,uint256[] buyAmounts,bytes32 hooksHash)"
)  # 0x9943e4ad53860ecdc48bb61f4f89cc48d27d46351af7bb72dbdd2f7f2a931859
# JamValidation: the EIP-712 domain with fields name, version, chainId and
# verifyingContract (eip712Domain().fields == 0x0f), name "JamSettlement",
# version "2".
EIP712_DOMAIN_TYPE_HASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
DOMAIN_NAME: constant(String[13]) = "JamSettlement"
DOMAIN_VERSION: constant(String[1]) = "2"

# JamHooks.EMPTY_HOOKS_HASH: an order without pre/post interactions.
EMPTY_HOOKS_HASH: constant(bytes32) = empty(bytes32)
# JamOrderLib.NATIVE_TOKEN: the native coin placeholder in token arrays.
NATIVE_TOKEN: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
# JamValidation.INF_EXPIRY: the expiry that marks a limit order (its nonce
# lives in a separate, cancellable space).
INF_EXPIRY: constant(uint256) = 9999999999

# JamPartner: partnerInfo packs [partner address (160 bits from bit 32) |
# partner fee bps (16 bits from bit 16) | protocol fee bps (16 bits)]; the
# fees together must stay below 100% and a partner address needs a partner
# fee. Fees come out of the solver's surplus, never out of the signed
# buyAmounts (the settlement requires the post-fee amount to cover them).
FEE_BPS_MASK: constant(uint256) = 2**16 - 1
PARTNER_FEE_OFFSET: constant(uint256) = 16
PARTNER_ADDRESS_OFFSET: constant(uint256) = 32
HUNDRED_PERCENT_BPS: constant(uint256) = 10000


# Hashing


@internal
@pure
def _domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    """
    @notice JamValidation.DOMAIN_SEPARATOR for the fixed name and version.
    """
    return keccak256(
        abi_encode(
            EIP712_DOMAIN_TYPE_HASH,
            keccak256(DOMAIN_NAME),
            keccak256(DOMAIN_VERSION),
            _chain_id,
            _verifying_contract,
        )
    )


@internal
@pure
def _hash_addresses(_values: DynArray[address, MAX_TOKENS]) -> bytes32:
    """
    @notice keccak256(abi.encodePacked(address[])): the elements padded to
            words and concatenated, the EIP-712 array encoding.
    """
    if len(_values) == 0:
        return keccak256(b"")
    # abi_encode of one dynamic argument is the tuple encoding: an offset
    # word and a length word ahead of the padded elements.
    encoded: Bytes[64 + MAX_TOKENS * 32] = abi_encode(_values)
    return keccak256(slice(encoded, 64, len(_values) * 32))


@internal
@pure
def _hash_amounts(_values: DynArray[uint256, MAX_TOKENS]) -> bytes32:
    """
    @notice keccak256(abi.encodePacked(uint256[])), as _hash_addresses.
    """
    if len(_values) == 0:
        return keccak256(b"")
    encoded: Bytes[64 + MAX_TOKENS * 32] = abi_encode(_values)
    return keccak256(slice(encoded, 64, len(_values) * 32))


@internal
@pure
def _struct_hash(_order: JamOrder) -> bytes32:
    """
    @notice JamOrderLib.hash(order, hooksHash): the EIP-712 hashStruct of
            the order, arrays hashed as their packed elements.
    """
    return keccak256(
        abi_encode(
            ORDER_TYPE_HASH,
            _order.taker,
            _order.receiver,
            _order.expiry,
            _order.exclusivityDeadline,
            _order.nonce,
            _order.executor,
            _order.partnerInfo,
            self._hash_addresses(_order.sellTokens),
            self._hash_addresses(_order.buyTokens),
            self._hash_amounts(_order.sellAmounts),
            self._hash_amounts(_order.buyAmounts),
            _order.hooksHash,
        )
    )


@internal
@pure
def _order_digest(_order: JamOrder, _domain_separator: bytes32) -> bytes32:
    """
    @notice The digest JamValidation.validateOrder passes to a contract
            taker's isValidSignature (and hashJamOrder returns for an order
            without Permit2): "\x19\x01" ++ domain ++ hashStruct.
    """
    return keccak256(concat(b"\x19\x01", _domain_separator, self._struct_hash(_order)))


@internal
@pure
def _to_calldata(_order: JamOrder) -> JamOrderCalldata:
    """
    @notice The order in the protocol's ABI shape, pulled with a plain
            transferFrom (usingPermit2 == False).
    """
    return JamOrderCalldata(
        taker=_order.taker,
        receiver=_order.receiver,
        expiry=_order.expiry,
        exclusivityDeadline=_order.exclusivityDeadline,
        nonce=_order.nonce,
        executor=_order.executor,
        partnerInfo=_order.partnerInfo,
        sellTokens=_order.sellTokens,
        buyTokens=_order.buyTokens,
        sellAmounts=_order.sellAmounts,
        buyAmounts=_order.buyAmounts,
        usingPermit2=False,
    )


# Partner info


@internal
@pure
def _unpack_partner_info(_partner_info: uint256) -> (address, uint256, uint256):
    """
    @notice JamPartner.unpackPartnerInfo: the partner address, the partner
            fee and the protocol fee (bps), with the protocol's validity
            rules (InvalidFeePercentage, InvalidPartnerAddress) mirrored as
            OrderNotValid reasons.
    """
    protocol_fee: uint256 = _partner_info & FEE_BPS_MASK
    partner_fee: uint256 = (_partner_info >> PARTNER_FEE_OFFSET) & FEE_BPS_MASK
    # The protocol reads the address as uint160: higher bits are dropped.
    partner: address = convert(
        convert((_partner_info >> PARTNER_ADDRESS_OFFSET) & (2**160 - 1), uint160), address
    )
    assert partner_fee + protocol_fee < HUNDRED_PERCENT_BPS, (
        OrderNotValid(reason="InvalidFeePercentage")
    )
    assert partner_fee > 0 or partner == empty(address), (
        OrderNotValid(reason="InvalidPartnerAddress")
    )
    return partner, partner_fee, protocol_fee
