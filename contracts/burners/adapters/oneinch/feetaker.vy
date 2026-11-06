# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title 1inch FeeTaker extension library
@author Curve Finance
@license MIT
@notice The byte layout of the FeeTaker extension (1inch/limit-order-protocol
        contracts/extensions/FeeTaker.sol with AmountGetterWithFee.sol, the
        public orderbook's only accepted order format, and the base of the
        Fusion settlement) and the shape checks the fee-taking 1inch adapters
        share: the amount getters, the post-interaction, the fee data and the
        resolver whitelist.
@dev Pure parametric helpers only: no storage, no external calls. Pinned to
     the deployed FeeTaker (0xc0DFdB9E7a392c3dBBE7c6FBe8FBC1789C9FE05e on
     Ethereum and Polygon, verified source identical to master up to the
     orderHash plumbing that does not touch the layout):
     amount getter extraData (after the 20-byte getter address, identical
     for the making and taking getters):
       2 bytes integrator fee (1e5), 1 byte integrator share (1e2),
       2 bytes resolver fee (1e5), 1 byte whitelist discount numerator (1e2),
       1 byte whitelist size, (bytes10)[N] low address halves,
       tail: a tail of 20 bytes or more chains another amount getter
       (AmountGetterBase), so the adapters require none.
     post-interaction extraData (after the 20-byte listener address):
       1 byte flags (0x01: custom receiver), 20 bytes integrator fee
       receiver, 20 bytes protocol fee receiver, [20 bytes custom receiver],
       the same fee data and whitelist as the getters (the Fusion
       settlement overrides the whitelist and appends a surplus block, see
       fusion.vy), tail: 20 bytes or more chain another IPostInteraction, so
       the adapters require none.
     Economics (AmountGetterWithFee, FeeTaker._postInteraction): the getter
     grosses the linear taking amount up by (1e5 + integrator fee +
     resolver fee) / 1e5, rounding up, and scales the making amount down by
     the same factor, rounding down; postInteraction, which runs only when
     the order receiver is the FeeTaker itself, keeps floor(T * fee /
     denominator) per fee and sends the rest to the custom receiver (or the
     maker). For a fill of making amount m the custom receiver thus gets at
     least ceil(order.takingAmount * m / order.makingAmount), the linear
     base, in every branch and at every rounding, provided the getter and
     the post-interaction read the same fees for the same taker: the
     adapters require identical fee bytes and whitelists on both sides.
"""

from contracts.burners.adapters.oneinch import oneinch


# The fee data of the getters, as AmountGetterWithFee._parseFeeData reads
# it.
struct Fees:
    integrator_fee: uint256  # 1e5 = 100% of the linear taking amount
    integrator_share: uint256  # 1e2 = 100% of the integrator fee, rest to the protocol
    resolver_fee: uint256  # 1e5 = 100% of the linear taking amount
    whitelist_discount: uint256  # 1e2 numerator: whitelisted takers pay this share of resolver_fee


# The receivers of the post-interaction header; custom is empty when the
# flag is unset (the taking tokens then go to the maker).
struct Receivers:
    integrator: address
    protocol: address
    custom: address


# Layout
TARGET_LEN: constant(uint256) = 20
FEE_DATA_LEN: constant(uint256) = 6
WHITELIST_SIZE_LEN: constant(uint256) = 1
WHITELIST_ENTRY_LEN: constant(uint256) = 10
# One byte of whitelist size.
MAX_WHITELIST: constant(uint256) = 255
CUSTOM_RECEIVER_FLAG: constant(uint256) = 1
# Flags and two fee receivers.
POST_HEADER_LEN: constant(uint256) = 1 + 2 * TARGET_LEN
BASE_1E5: constant(uint256) = 100_000
BASE_1E2: constant(uint256) = 100


# Extension fields


@internal
@pure
def _target_data(
    _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256, _field: uint256, _target: address
) -> (bool, Bytes[oneinch.MAX_EXTENSION]):
    """
    @notice The extraData of an extension field that targets a contract
            (amount getters, interactions): whether the field opens with
            `_target`, and the bytes after it.
    """
    data: Bytes[oneinch.MAX_EXTENSION] = oneinch._field(_extension, _offsets, _field)
    if len(data) < TARGET_LEN or convert(slice(data, 0, TARGET_LEN), address) != _target:
        return False, b""
    if len(data) == TARGET_LEN:
        return True, b""
    return True, slice(data, TARGET_LEN, len(data) - TARGET_LEN)


@internal
@pure
def _amount_getter_data(
    _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256, _getter: address
) -> Bytes[oneinch.MAX_EXTENSION]:
    """
    @notice The getter extraData: both amount getters must be `_getter` with
            identical extraData (the protocol passes each separately; a
            mismatch would price the two fill modes differently).
    """
    making_ok: bool = False
    making: Bytes[oneinch.MAX_EXTENSION] = b""
    making_ok, making = self._target_data(_extension, _offsets, oneinch.MAKING_AMOUNT_DATA, _getter)
    taking_ok: bool = False
    taking: Bytes[oneinch.MAX_EXTENSION] = b""
    taking_ok, taking = self._target_data(_extension, _offsets, oneinch.TAKING_AMOUNT_DATA, _getter)
    assert making_ok and taking_ok and making == taking, (
        oneinch.OrderNotValid(reason="BadAmountGetter")
    )
    return making


@internal
@pure
def _post_interaction_data(
    _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256, _listener: address
) -> Bytes[oneinch.MAX_EXTENSION]:
    """
    @notice The post-interaction extraData: the listener must be `_listener`
            (the protocol calls the maker itself for a field shorter than an
            address, which would leave the taking tokens with the FeeTaker).
    """
    ok: bool = False
    data: Bytes[oneinch.MAX_EXTENSION] = b""
    ok, data = self._target_data(_extension, _offsets, oneinch.POST_INTERACTION_DATA, _listener)
    assert ok, oneinch.OrderNotValid(reason="BadPostInteraction")
    return data


# Fee data and whitelist


@internal
@pure
def _parse_fees(_data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256) -> (Fees, uint256):
    """
    @notice The six fee bytes at `_pos`, with the bounds the getter reverts
            on (InvalidIntegratorShare, InvalidWhitelistDiscountNumerator)
            mirrored as BadFeeData; returns the fees and the next position.
    """
    assert len(_data) >= _pos + FEE_DATA_LEN, oneinch.OrderNotValid(reason="BadFeeData")
    fees: Fees = Fees(
        integrator_fee=convert(slice(_data, _pos, 2), uint256),
        integrator_share=convert(slice(_data, _pos + 2, 1), uint256),
        resolver_fee=convert(slice(_data, _pos + 3, 2), uint256),
        whitelist_discount=convert(slice(_data, _pos + 5, 1), uint256),
    )
    assert fees.integrator_share <= BASE_1E2 and fees.whitelist_discount <= BASE_1E2, (
        oneinch.OrderNotValid(reason="BadFeeData")
    )
    return fees, _pos + FEE_DATA_LEN


@internal
@pure
def _parse_whitelist(
    _data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256, _prefix_len: uint256, _entry_len: uint256
) -> (uint256, uint256, uint256):
    """
    @notice A whitelist at `_pos`: `_prefix_len` bytes of header, one byte of
            size, then size entries of `_entry_len` bytes each opening with
            the low ten bytes of a taker address. Returns the size, the
            position of the first entry and the next position.
    """
    size_pos: uint256 = _pos + _prefix_len
    assert len(_data) >= size_pos + WHITELIST_SIZE_LEN, oneinch.OrderNotValid(reason="BadWhitelist")
    size: uint256 = convert(slice(_data, size_pos, 1), uint256)
    entries: uint256 = size_pos + WHITELIST_SIZE_LEN
    end: uint256 = entries + size * _entry_len
    assert len(_data) >= end, oneinch.OrderNotValid(reason="BadWhitelist")
    return size, entries, end


@internal
@pure
def _check_same_whitelist(
    _getter: Bytes[oneinch.MAX_EXTENSION],
    _getter_entries: uint256,
    _post: Bytes[oneinch.MAX_EXTENSION],
    _post_entries: uint256,
    _post_entry_len: uint256,
    _size: uint256,
):
    """
    @notice The `_size` address halves of the post-interaction whitelist
            must be the getters' halves in the same order, so a taker is
            whitelisted (and discounted) on both sides or on neither.
    """
    for i: uint256 in range(_size, bound=MAX_WHITELIST):
        assert slice(_getter, _getter_entries + i * WHITELIST_ENTRY_LEN, WHITELIST_ENTRY_LEN) == (
            slice(_post, _post_entries + i * _post_entry_len, WHITELIST_ENTRY_LEN)
        ), oneinch.OrderNotValid(reason="BadWhitelist")


@internal
@pure
def _check_fee_block(
    _getter: Bytes[oneinch.MAX_EXTENSION],
    _getter_pos: uint256,
    _post: Bytes[oneinch.MAX_EXTENSION],
    _post_pos: uint256,
    _post_whitelist_prefix_len: uint256,
    _post_whitelist_entry_len: uint256,
) -> (Fees, uint256, uint256, uint256):
    """
    @notice The fee data and whitelist of the getters (at `_getter_pos`) and
            of the post-interaction (at `_post_pos`): both parse, the fee
            bytes are identical and the whitelists list the same takers, so
            what the taker is charged is what the post-interaction splits.
            Returns the fees, the whitelist size and the next positions on
            both sides.
    """
    fees: Fees = empty(Fees)
    getter_pos: uint256 = 0
    fees, getter_pos = self._parse_fees(_getter, _getter_pos)
    post_pos: uint256 = 0
    fees, post_pos = self._parse_fees(_post, _post_pos)
    assert slice(_getter, _getter_pos, FEE_DATA_LEN) == slice(_post, _post_pos, FEE_DATA_LEN), (
        oneinch.OrderNotValid(reason="BadFeeData")
    )
    size: uint256 = 0
    getter_entries: uint256 = 0
    size, getter_entries, getter_pos = self._parse_whitelist(
        _getter, getter_pos, 0, WHITELIST_ENTRY_LEN
    )
    post_size: uint256 = 0
    post_entries: uint256 = 0
    post_size, post_entries, post_pos = self._parse_whitelist(
        _post, post_pos, _post_whitelist_prefix_len, _post_whitelist_entry_len
    )
    assert post_size == size, oneinch.OrderNotValid(reason="BadWhitelist")
    self._check_same_whitelist(
        _getter, getter_entries, _post, post_entries, _post_whitelist_entry_len, size
    )
    return fees, size, getter_pos, post_pos


# Post-interaction header


@internal
@pure
def _parse_post_header(_data: Bytes[oneinch.MAX_EXTENSION]) -> (Receivers, uint256):
    """
    @notice The flags and receivers opening the post-interaction extraData,
            as FeeTaker._postInteraction reads them; returns the receivers
            (custom empty when the flag is unset) and the next position.
    """
    assert len(_data) >= POST_HEADER_LEN, oneinch.OrderNotValid(reason="BadPostInteraction")
    flags: uint256 = convert(slice(_data, 0, 1), uint256)
    receivers: Receivers = Receivers(
        integrator=convert(slice(_data, 1, TARGET_LEN), address),
        protocol=convert(slice(_data, 1 + TARGET_LEN, TARGET_LEN), address),
        custom=empty(address),
    )
    pos: uint256 = POST_HEADER_LEN
    if flags & CUSTOM_RECEIVER_FLAG != 0:
        assert len(_data) >= POST_HEADER_LEN + TARGET_LEN, (
            oneinch.OrderNotValid(reason="BadPostInteraction")
        )
        receivers.custom = convert(slice(_data, POST_HEADER_LEN, TARGET_LEN), address)
        pos += TARGET_LEN
    return receivers, pos


@internal
@pure
def _taking_receiver(_receivers: Receivers, _maker: address) -> address:
    """
    @notice Where the post-interaction sends the taking tokens net of fees:
            the custom receiver when flagged, else the maker.
    """
    if _receivers.custom == empty(address):
        return _maker
    return _receivers.custom
