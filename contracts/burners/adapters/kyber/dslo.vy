# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title KyberSwap DSLO order library
@author Curve Finance
@license MIT
@notice Stateless KyberSwap Double Signature Limit Order (DSLO) order type,
        its EIP-712 digest and domain, the feeConfig bit layout, the linear
        amount getters, the predicate shapes carrying an expiry, and the
        OrderNotValid revert shared by Kyber-facing adapters.
@dev Pure parametric helpers only: no storage, no external calls, no abstract
     hooks. Hashing must stay byte-identical to DSOrderMixin.hashOrder, the
     getters to AmountCalculator, the predicates to PredicateHelper and
     NonceManager (DSLOProtocol, a 1inch Limit Order Protocol v2 fork,
     0xcab2FA2eeab7065B45CBcF6E3936dDE2506b4f6C on every chain it serves).
"""


# Protocol-level order rejection (selector OrderNotValid(string), the same
# shape the CoW and 1inch adapters use) so every adapter reports shape and
# constant failures the same way.
error OrderNotValid:
    reason: String[32]


# Bounds of the variable-sized order fields. Asset data must be empty
# (anything up to the bound decodes and is rejected as such; longer fails
# ABI decoding), a getter is empty or the 68-byte linear call, a predicate
# is `timestampBelow(t)` or an `and` over at most MAX_PREDICATES entries of
# `nonceEquals(maker, nonce)` and `timestampBelow(t)`, and the interaction
# must be empty.
MAX_ASSET_DATA: constant(uint256) = 32
GETTER_LEN: constant(uint256) = 4 + 2 * 32
MAX_PREDICATES: constant(uint256) = 4
# nonceEquals(address,uint256): the longest accepted predicate entry.
MAX_PREDICATE_CALL: constant(uint256) = 4 + 2 * 32
TIMESTAMP_BELOW_LEN: constant(uint256) = 4 + 32
# `and(address[],bytes[])` at the bound: selector, two offsets, two length
# words, MAX_PREDICATES targets, MAX_PREDICATES data offsets, and every
# entry a length word plus its data padded to three words.
MAX_PREDICATE: constant(uint256) = 4 + 32 * (2 + 2 + 2 * MAX_PREDICATES + 4 * MAX_PREDICATES)
MAX_INTERACTION: constant(uint256) = 64


# IDSOrderMixin.Order.
struct Order:
    salt: uint256
    makerAsset: address
    takerAsset: address
    maker: address
    receiver: address
    allowedSender: address
    makingAmount: uint256
    takingAmount: uint256
    feeConfig: uint256
    makerAssetData: Bytes[MAX_ASSET_DATA]
    takerAssetData: Bytes[MAX_ASSET_DATA]
    getMakerAmount: Bytes[GETTER_LEN]
    getTakerAmount: Bytes[GETTER_LEN]
    predicate: Bytes[MAX_PREDICATE]
    interaction: Bytes[MAX_INTERACTION]


# abi_encode(Order) at the bound: the tuple offset, nine static words, six
# field offsets, and every field's length word plus its bound padded to
# whole words.
ENCODED_ORDER_BOUND: constant(uint256) = (
    32
    + 9 * 32
    + 6 * 32
    + 2 * (32 + MAX_ASSET_DATA)
    + 2 * (32 + 3 * 32)
    + (32 + (MAX_PREDICATE + 31) // 32 * 32)
    + (32 + MAX_INTERACTION)
)

# DSOrderMixin.LIMIT_ORDER_TYPEHASH.
ORDER_TYPE_HASH: constant(bytes32) = keccak256(
    "Order(uint256 salt,address makerAsset,address takerAsset,address maker,address receiver,"
    "address allowedSender,uint256 makingAmount,uint256 takingAmount,uint256 feeConfig,"
    "bytes makerAssetData,bytes takerAssetData,bytes getMakerAmount,bytes getTakerAmount,"
    "bytes predicate,bytes interaction)"
)  # 0x346e0660be2aef32794b25da30f6733303978cb6c10c395480eb5d2c56bcd956
# OpenZeppelin EIP712 domain: DSLOProtocol is EIP712('Kyber DSLO Protocol', '1').
EIP712_DOMAIN_TYPE_HASH: constant(bytes32) = keccak256(
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
DOMAIN_NAME_HASH: constant(bytes32) = keccak256("Kyber DSLO Protocol")
DOMAIN_VERSION_HASH: constant(bytes32) = keccak256("1")

# feeConfig: feeRecipient in the low 160 bits, amountTokenFeePercent (units
# of 1e-5) in the 32 bits from bit 160, isTakerAssetFee in the bits from
# 192. A fee is charged only when both recipient and percent are nonzero.
FEE_RECIPIENT_MASK: constant(uint256) = 2**160 - 1
FEE_PERCENT_OFFSET: constant(uint256) = 160
FEE_PERCENT_MASK: constant(uint256) = 2**32 - 1
TAKER_ASSET_FEE_OFFSET: constant(uint256) = 192
FEE_UNITS: constant(uint256) = 100_000

# AmountCalculator and PredicateHelper/NonceManager selectors, all called on
# the protocol itself with a staticcall.
GET_MAKER_AMOUNT_SELECTOR: constant(bytes4) = method_id(
    "getMakerAmount(uint256,uint256,uint256)", output_type=bytes4
)  # 0xf4a215c3
GET_TAKER_AMOUNT_SELECTOR: constant(bytes4) = method_id(
    "getTakerAmount(uint256,uint256,uint256)", output_type=bytes4
)  # 0x296637bf
AND_SELECTOR: constant(bytes4) = method_id(
    "and(address[],bytes[])", output_type=bytes4
)  # 0x961d5b1e
NONCE_EQUALS_SELECTOR: constant(bytes4) = method_id(
    "nonceEquals(address,uint256)", output_type=bytes4
)  # 0xcf6fc6e3
TIMESTAMP_BELOW_SELECTOR: constant(bytes4) = method_id(
    "timestampBelow(uint256)", output_type=bytes4
)  # 0x63592c2b


# Hashing


@internal
@pure
def _domain_separator(_chain_id: uint256, _verifying_contract: address) -> bytes32:
    """
    @notice OpenZeppelin EIP712._buildDomainSeparator for the protocol's
            fixed name and version.
    """
    return keccak256(
        abi_encode(
            EIP712_DOMAIN_TYPE_HASH,
            DOMAIN_NAME_HASH,
            DOMAIN_VERSION_HASH,
            _chain_id,
            _verifying_contract,
        )
    )


@internal
@pure
def _order_digest(_order: Order, _domain_separator: bytes32) -> bytes32:
    """
    @notice DSOrderMixin.hashOrder: the EIP-712 digest the protocol asks the
            maker to sign and passes to a contract maker's isValidSignature
            (the static fields in place, every bytes field by its hash).
    """
    struct_hash: bytes32 = keccak256(
        abi_encode(
            ORDER_TYPE_HASH,
            _order.salt,
            _order.makerAsset,
            _order.takerAsset,
            _order.maker,
            _order.receiver,
            _order.allowedSender,
            _order.makingAmount,
            _order.takingAmount,
            _order.feeConfig,
            keccak256(_order.makerAssetData),
            keccak256(_order.takerAssetData),
            keccak256(_order.getMakerAmount),
            keccak256(_order.getTakerAmount),
            keccak256(_order.predicate),
            keccak256(_order.interaction),
        )
    )
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


# Order fields


@internal
@pure
def _receiver(_order: Order) -> address:
    """
    @notice The taker-asset receiver as the protocol resolves it: an empty
            receiver means the maker.
    """
    if _order.receiver == empty(address):
        return _order.maker
    return _order.receiver


@internal
@pure
def _fee_config(_fee_config: uint256) -> (address, uint256, bool):
    """
    @notice Unpack feeConfig the way the protocol does: recipient, percent
            (1e-5 units) and whether the fee is charged on the taker asset.
    """
    return (
        convert(_fee_config & FEE_RECIPIENT_MASK, address),
        (_fee_config >> FEE_PERCENT_OFFSET) & FEE_PERCENT_MASK,
        (_fee_config >> TAKER_ASSET_FEE_OFFSET) != 0,
    )


# Amount getters: AmountCalculator's linear ones over the order's own
# amounts, the swap amount appended by the protocol at fill time. The
# protocol prices a fill with whatever call the getter bytes encode, so an
# adapter accepts only these (or an empty getter, which allows exact-amount
# fills only).


@internal
@pure
def _linear_getter(_selector: bytes4, _order: Order) -> Bytes[GETTER_LEN]:
    return concat(_selector, abi_encode(_order.makingAmount, _order.takingAmount))


@internal
@pure
def _check_getters(_order: Order):
    """
    @notice Require each getter to be empty or the linear call over the
            order's own amounts: the taking amount of a making-amount fill
            is then ceil(making * takingAmount / makingAmount) and the
            making amount of a taking-amount fill floor(taking *
            makingAmount / takingAmount), both in the maker's favour.
    """
    assert len(_order.getMakerAmount) == 0 or (
        _order.getMakerAmount == self._linear_getter(GET_MAKER_AMOUNT_SELECTOR, _order)
    ), OrderNotValid(reason="BadAmountGetter")
    assert len(_order.getTakerAmount) == 0 or (
        _order.getTakerAmount == self._linear_getter(GET_TAKER_AMOUNT_SELECTOR, _order)
    ), OrderNotValid(reason="BadAmountGetter")


# Predicates: the protocol staticcalls itself with the predicate bytes and
# requires a true result. The order carries no expiry field; PredicateHelper's
# `timestampBelow(t)` (block.timestamp < t) is the expiry, alone or as an
# entry of `and(address[] targets, bytes[] data)` next to NonceManager's
# `nonceEquals(maker, nonce)` entries (the shape the KyberSwap API builds).


@internal
@pure
def _timestamp_below(_time: uint256) -> Bytes[TIMESTAMP_BELOW_LEN]:
    return concat(TIMESTAMP_BELOW_SELECTOR, abi_encode(_time))


@internal
@pure
def _nonce_equals(_maker: address, _nonce: uint256) -> Bytes[MAX_PREDICATE_CALL]:
    return concat(NONCE_EQUALS_SELECTOR, abi_encode(_maker, _nonce))


@internal
@pure
def _timestamp_below_arg(_call: Bytes[MAX_PREDICATE_CALL]) -> uint256:
    """
    @notice The time of a canonical `timestampBelow(t)` call; reverts with
            BadPredicate for any other bytes.
    """
    assert len(_call) == TIMESTAMP_BELOW_LEN and (
        convert(slice(_call, 0, 4), bytes4) == TIMESTAMP_BELOW_SELECTOR
    ), OrderNotValid(reason="BadPredicate")
    return convert(slice(_call, 4, 32), uint256)


@internal
@pure
def _valid_to(_order: Order, _protocol: address) -> uint256:
    """
    @notice The last timestamp the predicate lets the order fill at: t - 1
            for its `timestampBelow(t)`. Accepts exactly the shapes above —
            a bare `timestampBelow`, or a canonical `and` over the protocol
            itself whose entries are `nonceEquals(order.maker, nonce)` (any
            nonce: it only gates the fill) and exactly one
            `timestampBelow` — and reverts with BadPredicate for anything
            else, including an empty predicate (a never-expiring order).
    """
    predicate: Bytes[MAX_PREDICATE] = _order.predicate
    assert len(predicate) > 4, OrderNotValid(reason="BadPredicate")
    selector: bytes4 = convert(slice(predicate, 0, 4), bytes4)
    if selector == TIMESTAMP_BELOW_SELECTOR:
        assert len(predicate) == TIMESTAMP_BELOW_LEN, OrderNotValid(reason="BadPredicate")
        return convert(slice(predicate, 4, 32), uint256) - 1  # dev: timestampBelow(0)
    assert selector == AND_SELECTOR, OrderNotValid(reason="BadPredicate")

    targets: DynArray[address, MAX_PREDICATES] = []
    data: DynArray[Bytes[MAX_PREDICATE_CALL], MAX_PREDICATES] = []
    targets, data = abi_decode(
        slice(predicate, 4, len(predicate) - 4),
        (DynArray[address, MAX_PREDICATES], DynArray[Bytes[MAX_PREDICATE_CALL], MAX_PREDICATES]),
    )
    assert (
        concat(AND_SELECTOR, abi_encode(targets, data)) == predicate
        and len(targets) == len(data)
        and len(targets) > 0
    ), OrderNotValid(reason="BadPredicate")

    time: uint256 = 0
    found: bool = False
    for i: uint256 in range(MAX_PREDICATES):
        if i >= len(targets):
            break
        assert targets[i] == _protocol, OrderNotValid(reason="BadPredicate")
        call: Bytes[MAX_PREDICATE_CALL] = data[i]
        assert len(call) >= 4, OrderNotValid(reason="BadPredicate")
        if convert(slice(call, 0, 4), bytes4) == NONCE_EQUALS_SELECTOR:
            # The nonce is free; the maker word must be the order maker (a
            # word with dirty upper bits is not).
            assert len(call) == MAX_PREDICATE_CALL and (
                convert(slice(call, 4, 32), uint256) == convert(_order.maker, uint256)
            ), OrderNotValid(reason="BadPredicate")
        else:
            assert not found, OrderNotValid(reason="BadPredicate")
            time = self._timestamp_below_arg(call)
            found = True
    assert found, OrderNotValid(reason="BadPredicate")
    return time - 1  # dev: timestampBelow(0)
