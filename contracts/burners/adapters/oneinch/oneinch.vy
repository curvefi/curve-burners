# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title 1inch Limit Order Protocol v4 library
@author Curve Finance
@license MIT
@notice Stateless 1inch LOP v4 order type, its EIP-712 digest and domain,
        the makerTraits bit layout, the extension binding and field layout,
        and the OrderNotValid revert shared by 1inch-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to OrderLib.hash, the traits to
     MakerTraitsLib, the extension rules to OrderLib.isValidExtension and
     ExtensionLib/OffsetsLib (1inch/limit-order-protocol v4, deployed as
     AggregationRouterV6).
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape CoW-facing adapters use) so every adapter reports
# shape and constant failures the same way.
error OrderNotValid:
    reason: String[32]


# IOrderMixin.Order. The protocol declares the address fields as its
# uint256-backed Address type; the EIP-712 type names them address and the
# hash covers the raw words, so a word with dirty upper bits hashes
# differently from its masked address and never matches a decoded order.
struct Order:
    salt: uint256
    maker: address
    receiver: address
    makerAsset: address
    takerAsset: address
    makingAmount: uint256
    takingAmount: uint256
    makerTraits: uint256


# The same order as the protocol ABI spells it: every field a uint256 word
# (its Address type), so calls into the protocol select its functions.
struct OrderWords:
    salt: uint256
    maker: uint256
    receiver: uint256
    makerAsset: uint256
    takerAsset: uint256
    makingAmount: uint256
    takingAmount: uint256
    makerTraits: uint256


# Every field is a static ABI type, so the struct encodes to 8 words.
ENCODED_ORDER_LEN: constant(uint256) = 8 * 32
# Decoding bound for an order extension carried next to the order. Only
# extensions without amount getters are accepted (see the adapter), which
# leaves predicates, permits and interactions; longer ones fail ABI decoding
# before any check runs.
MAX_EXTENSION: constant(uint256) = 1024

# OrderLib._LIMIT_ORDER_TYPEHASH.
ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "Order(uint256 salt,address maker,address receiver,address makerAsset,address takerAsset,"
    "uint256 makingAmount,uint256 takingAmount,uint256 makerTraits)"
)  # 0x3af21ec5a20011b88d3b7b4ed7c806cef05a5980cf34974bcd53566a131f7e4c
# OpenZeppelin EIP712 domain with fields name, version, chainId and
# verifyingContract (eip712Domain().fields == 0x0f).
EIP712_DOMAIN_TYPE_HASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
EIP712_DOMAIN_FIELDS: constant(bytes1) = 0x0f
MAX_DOMAIN_NAME: constant(uint256) = 64
MAX_DOMAIN_VERSION: constant(uint256) = 32

# MakerTraitsLib: flags in the high bits.
NO_PARTIAL_FILLS_FLAG: constant(uint256) = 1 << 255
ALLOW_MULTIPLE_FILLS_FLAG: constant(uint256) = 1 << 254
PRE_INTERACTION_CALL_FLAG: constant(uint256) = 1 << 252
POST_INTERACTION_CALL_FLAG: constant(uint256) = 1 << 251
NEED_CHECK_EPOCH_MANAGER_FLAG: constant(uint256) = 1 << 250
HAS_EXTENSION_FLAG: constant(uint256) = 1 << 249
USE_PERMIT2_FLAG: constant(uint256) = 1 << 248
UNWRAP_WETH_FLAG: constant(uint256) = 1 << 247
# MakerTraitsLib: the low 200 bits hold the allowed sender (80 bits), the
# expiration (40 bits from bit 80; 0 means none), nonce or epoch (40 bits
# from bit 120) and series (40 bits from bit 160).
EXPIRATION_OFFSET: constant(uint256) = 80
EXPIRATION_MASK: constant(uint256) = 2**40 - 1

# OrderLib.isValidExtension: the low 160 bits of the salt commit to the
# extension hash.
EXTENSION_HASH_MASK: constant(uint256) = 2**160 - 1
# ExtensionLib/OffsetsLib: the first word of an extension of at least 32
# bytes packs eight uint32 cumulative field ends, field i ending at bits
# [32 i, 32 i + 32) and starting where field i - 1 ends; the fields follow
# the word in this order, custom data after the last one. A shorter
# extension has every field empty.
EXTENSION_FIELDS: constant(uint256) = 8
FIELD_END_MASK: constant(uint256) = 2**32 - 1
MAKER_ASSET_SUFFIX: constant(uint256) = 0
TAKER_ASSET_SUFFIX: constant(uint256) = 1
MAKING_AMOUNT_DATA: constant(uint256) = 2
TAKING_AMOUNT_DATA: constant(uint256) = 3
PREDICATE: constant(uint256) = 4
MAKER_PERMIT: constant(uint256) = 5
PRE_INTERACTION_DATA: constant(uint256) = 6
POST_INTERACTION_DATA: constant(uint256) = 7


# Hashing


@internal
@pure
def _domain_separator(
    _name: String[MAX_DOMAIN_NAME],
    _version: String[MAX_DOMAIN_VERSION],
    _chain_id: uint256,
    _verifying_contract: address,
) -> bytes32:
    """
    @notice OpenZeppelin EIP712._buildDomainSeparator for the four-field
            domain the protocol reports through eip712Domain().
    """
    return keccak256(
        abi_encode(
            EIP712_DOMAIN_TYPE_HASH,
            keccak256(_name),
            keccak256(_version),
            _chain_id,
            _verifying_contract,
        )
    )


@internal
@pure
def _order_digest(_order: Order, _domain_separator: bytes32) -> bytes32:
    """
    @notice OrderLib.hash: the EIP-712 digest the protocol asks the maker to
            sign and passes to a contract maker's isValidSignature.
    """
    # All order fields are static types, so abi_encode(struct) is exactly the
    # word-per-field layout hashStruct expects.
    struct_hash: bytes32 = keccak256(abi_encode(ORDER_TYPE_HASH, _order))
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


@internal
@pure
def _to_words(_order: Order) -> OrderWords:
    """
    @notice The order in the protocol's ABI shape (addresses as words).
    """
    return OrderWords(
        salt=_order.salt,
        maker=convert(_order.maker, uint256),
        receiver=convert(_order.receiver, uint256),
        makerAsset=convert(_order.makerAsset, uint256),
        takerAsset=convert(_order.takerAsset, uint256),
        makingAmount=_order.makingAmount,
        takingAmount=_order.takingAmount,
        makerTraits=_order.makerTraits,
    )


@internal
@pure
def _words_digest(_words: OrderWords, _domain_separator: bytes32) -> bytes32:
    """
    @notice OrderLib.hash over the raw words, as the protocol computes it
            (dirty upper bits included); equal to _order_digest for a
            clean order.
    """
    struct_hash: bytes32 = keccak256(abi_encode(ORDER_TYPE_HASH, _words))
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


# Maker traits


@internal
@pure
def _has_flag(_maker_traits: uint256, _flag: uint256) -> bool:
    return _maker_traits & _flag != 0


@internal
@pure
def _expiration(_maker_traits: uint256) -> uint256:
    """
    @notice MakerTraitsLib.getExpirationTime: the last timestamp the
            protocol fills at (isExpired is strict); 0 means never expires.
    """
    return (_maker_traits >> EXPIRATION_OFFSET) & EXPIRATION_MASK


@internal
@pure
def _receiver(_order: Order) -> address:
    """
    @notice OrderLib.getReceiver: an empty receiver means the maker.
    """
    if _order.receiver == empty(address):
        return _order.maker
    return _order.receiver


# Extension


@internal
@pure
def _check_extension_binding(_order: Order, _extension: Bytes[MAX_EXTENSION]):
    """
    @notice OrderLib.isValidExtension: an order with the extension flag
            carries a non-empty extension whose hash the salt commits to;
            one without carries none. Reverts with the protocol's error
            names as reasons.
    """
    if self._has_flag(_order.makerTraits, HAS_EXTENSION_FLAG):
        assert len(_extension) > 0, OrderNotValid(reason="MissingOrderExtension")
        assert (
            convert(keccak256(_extension), uint256) & EXTENSION_HASH_MASK
            == _order.salt & EXTENSION_HASH_MASK
        ), OrderNotValid(reason="InvalidExtensionHash")
    else:
        assert len(_extension) == 0, OrderNotValid(reason="UnexpectedOrderExtension")


@internal
@pure
def _extension_offsets(_extension: Bytes[MAX_EXTENSION]) -> uint256:
    """
    @notice The packed field ends of an extension; zero (every field empty)
            for one shorter than a word, as ExtensionLib reads it.
    """
    if len(_extension) < 32:
        return 0
    return convert(slice(_extension, 0, 32), uint256)


@internal
@pure
def _field_is_empty(_offsets: uint256, _field: uint256) -> bool:
    """
    @notice Whether extension field `_field` (0..7) is empty: its end equals
            the previous field's end (0 for the first). An end below its
            start is not empty either (the protocol reads it as a bogus
            length), so only a well-formed empty field passes.
    """
    end: uint256 = (_offsets >> (32 * _field)) & FIELD_END_MASK
    begin: uint256 = 0
    if _field > 0:
        begin = (_offsets >> (32 * (_field - 1))) & FIELD_END_MASK
    return end == begin
