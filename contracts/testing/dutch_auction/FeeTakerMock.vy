# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title FeeTakerMock
@author Curve Finance
@license MIT
@notice Test double of the deployed 1inch FeeTaker (`_fusion = False`) and
        of the Fusion SimpleSettlement (`_fusion = True`): the fee-grossed
        amount getters and the fee-splitting post-interaction with the
        verified byte layouts, rounding and access rule, so adapter tests can
        settle orders through LimitOrderProtocolMock and check what reaches
        every receiver. Not modelled: the native (WETH) paths, chained
        getters and interactions (the adapters reject all of them) and the
        Ethereum Settlement's priority-fee cap.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.oneinch import oneinch


interface AccessToken:
    def balanceOf(_owner: address) -> uint256: view


error OnlyLimitOrderProtocol:
    pass


error OnlyWhitelistOrAccessToken:
    pass


error InconsistentFee:
    pass


error InvalidIntegratorShare:
    pass


error InvalidWhitelistDiscountNumerator:
    pass


error AllowedTimeViolation:
    pass


error InvalidProtocolSurplusFee:
    pass


error InvalidEstimatedTakingAmount:
    pass


error NotModelled:
    pass


MAX_DATA: constant(uint256) = oneinch.MAX_EXTENSION
BASE_1E5: constant(uint256) = 100_000
BASE_1E2: constant(uint256) = 100
BASE_POINTS: constant(uint256) = 10_000_000
GAS_PRICE_BASE: constant(uint256) = 1_000_000
MAX_LIST: constant(uint256) = 255
CUSTOM_RECEIVER_FLAG: constant(uint256) = 1
HALF_MASK: constant(uint256) = 2**80 - 1

limit_order_protocol: public(immutable(address))
access_token: public(immutable(address))
fusion: public(immutable(bool))


@deploy
def __init__(_limit_order_protocol: address, _access_token: address, _fusion: bool):
    self.limit_order_protocol = _limit_order_protocol
    self.access_token = _access_token
    self.fusion = _fusion


# Shared parsing


@internal
@pure
def _ceil_div(_a: uint256, _b: uint256, _d: uint256) -> uint256:
    return (_a * _b + _d - 1) // _d


@internal
@view
def _whitelisted_getter(_data: Bytes[MAX_DATA], _pos: uint256, _taker: address) -> (bool, uint256):
    """AmountGetterWithFee._isWhitelistedGetterImpl: 1 byte size, (bytes10)[N]."""
    half: uint256 = convert(_taker, uint256) & HALF_MASK
    size: uint256 = convert(slice(_data, _pos, 1), uint256)
    whitelisted: bool = False
    for i: uint256 in range(size, bound=MAX_LIST):
        if convert(slice(_data, _pos + 1 + 10 * i, 10), uint256) == half:
            whitelisted = True
    return whitelisted, _pos + 1 + 10 * size


@internal
@view
def _whitelisted_post(_data: Bytes[MAX_DATA], _pos: uint256, _taker: address) -> (bool, uint256):
    """FeeTaker reuses the getter list; SimpleSettlement reads 4 bytes allowed
    time, 1 byte size, (bytes10 half, bytes2 delta)[N] with time gating."""
    if not self.fusion:
        return self._whitelisted_getter(_data, _pos, _taker)
    half: uint256 = convert(_taker, uint256) & HALF_MASK
    allowed_time: uint256 = convert(slice(_data, _pos, 4), uint256)
    size: uint256 = convert(slice(_data, _pos + 4, 1), uint256)
    entries: uint256 = _pos + 5
    for i: uint256 in range(size, bound=MAX_LIST):
        assert block.timestamp >= allowed_time, AllowedTimeViolation()
        if convert(slice(_data, entries + 12 * i, 10), uint256) == half:
            return True, entries + 12 * size
        allowed_time += convert(slice(_data, entries + 12 * i + 10, 2), uint256)
    assert block.timestamp >= allowed_time, AllowedTimeViolation()
    return False, entries + 12 * size


@internal
@view
def _parse_fee_data(
    _data: Bytes[MAX_DATA], _pos: uint256, _taker: address, _post: bool
) -> (bool, uint256, uint256, uint256, uint256):
    """AmountGetterWithFee._parseFeeData: (whitelisted, integrator fee,
    integrator share, discounted resolver fee, next position)."""
    integrator_fee: uint256 = convert(slice(_data, _pos, 2), uint256)
    integrator_share: uint256 = convert(slice(_data, _pos + 2, 1), uint256)
    assert integrator_share <= BASE_1E2, InvalidIntegratorShare()
    resolver_fee: uint256 = convert(slice(_data, _pos + 3, 2), uint256)
    discount: uint256 = convert(slice(_data, _pos + 5, 1), uint256)
    assert discount <= BASE_1E2, InvalidWhitelistDiscountNumerator()
    whitelisted: bool = False
    next_pos: uint256 = 0
    if _post:
        whitelisted, next_pos = self._whitelisted_post(_data, _pos + 6, _taker)
    else:
        whitelisted, next_pos = self._whitelisted_getter(_data, _pos + 6, _taker)
    if whitelisted:
        resolver_fee = resolver_fee * discount // BASE_1E2
    return whitelisted, integrator_fee, integrator_share, resolver_fee, next_pos


@internal
@view
def _rate_bump(_data: Bytes[MAX_DATA]) -> (uint256, uint256):
    """SimpleSettlement._getRateBump / _getAuctionBump; (0, 0) for a FeeTaker."""
    if not self.fusion:
        return 0, 0
    gas_bump_estimate: uint256 = convert(slice(_data, 0, 3), uint256)
    gas_price_estimate: uint256 = convert(slice(_data, 3, 4), uint256)
    gas_bump: uint256 = 0
    if gas_bump_estimate != 0 and gas_price_estimate != 0:
        gas_bump = gas_bump_estimate * block.basefee // gas_price_estimate // GAS_PRICE_BASE
    start: uint256 = convert(slice(_data, 7, 4), uint256)
    finish: uint256 = start + convert(slice(_data, 11, 3), uint256)
    current_bump: uint256 = convert(slice(_data, 14, 3), uint256)
    count: uint256 = convert(slice(_data, 17, 1), uint256)
    tail: uint256 = 18 + 5 * count
    auction_bump: uint256 = 0
    if block.timestamp <= start:
        auction_bump = current_bump
    elif block.timestamp >= finish:
        auction_bump = 0
    else:
        current_time: uint256 = start
        found: bool = False
        for i: uint256 in range(count, bound=MAX_LIST):
            if found:
                continue
            next_bump: uint256 = convert(slice(_data, 18 + 5 * i, 3), uint256)
            next_time: uint256 = current_time + convert(slice(_data, 18 + 5 * i + 3, 2), uint256)
            if block.timestamp <= next_time:
                auction_bump = (
                    (block.timestamp - current_time) * next_bump
                    + (next_time - block.timestamp) * current_bump
                ) // (next_time - current_time)
                found = True
            else:
                current_bump = next_bump
                current_time = next_time
        if not found:
            auction_bump = (finish - block.timestamp) * current_bump // (finish - current_time)
    if auction_bump > gas_bump:
        return auction_bump - gas_bump, tail
    return 0, tail


# Amount getters


@external
@view
def getTakingAmount(
    _order: oneinch.OrderWords,
    _extension: Bytes[MAX_DATA],
    _order_hash: bytes32,
    _taker: address,
    _making_amount: uint256,
    _remaining_making_amount: uint256,
    _extra_data: Bytes[MAX_DATA],
) -> uint256:
    bump: uint256 = 0
    pos: uint256 = 0
    bump, pos = self._rate_bump(_extra_data)
    integrator_fee: uint256 = 0
    resolver_fee: uint256 = 0
    whitelisted: bool = False
    share: uint256 = 0
    whitelisted, integrator_fee, share, resolver_fee, pos = self._parse_fee_data(
        _extra_data, pos, _taker, False
    )
    assert len(_extra_data) - pos < 20, NotModelled()
    amount: uint256 = self._ceil_div(_order.takingAmount, _making_amount, _order.makingAmount)
    amount = self._ceil_div(amount, BASE_1E5 + integrator_fee + resolver_fee, BASE_1E5)
    if self.fusion:
        amount = self._ceil_div(amount, BASE_POINTS + bump, BASE_POINTS)
    return amount


@external
@view
def getMakingAmount(
    _order: oneinch.OrderWords,
    _extension: Bytes[MAX_DATA],
    _order_hash: bytes32,
    _taker: address,
    _taking_amount: uint256,
    _remaining_making_amount: uint256,
    _extra_data: Bytes[MAX_DATA],
) -> uint256:
    bump: uint256 = 0
    pos: uint256 = 0
    bump, pos = self._rate_bump(_extra_data)
    integrator_fee: uint256 = 0
    resolver_fee: uint256 = 0
    whitelisted: bool = False
    share: uint256 = 0
    whitelisted, integrator_fee, share, resolver_fee, pos = self._parse_fee_data(
        _extra_data, pos, _taker, False
    )
    assert len(_extra_data) - pos < 20, NotModelled()
    amount: uint256 = _order.makingAmount * _taking_amount // _order.takingAmount
    amount = amount * BASE_1E5 // (BASE_1E5 + integrator_fee + resolver_fee)
    if self.fusion:
        amount = amount * BASE_POINTS // (BASE_POINTS + bump)
    return amount


# Post-interaction


@external
def postInteraction(
    _order: oneinch.OrderWords,
    _extension: Bytes[MAX_DATA],
    _order_hash: bytes32,
    _taker: address,
    _making_amount: uint256,
    _taking_amount: uint256,
    _remaining_making_amount: uint256,
    _extra_data: Bytes[MAX_DATA],
):
    assert msg.sender == self.limit_order_protocol, OnlyLimitOrderProtocol()
    flags: uint256 = convert(slice(_extra_data, 0, 1), uint256)
    integrator: address = convert(slice(_extra_data, 1, 20), address)
    protocol: address = convert(slice(_extra_data, 21, 20), address)
    receiver: address = convert(_order.maker, address)
    pos: uint256 = 41
    if flags & CUSTOM_RECEIVER_FLAG != 0:
        receiver = convert(slice(_extra_data, 41, 20), address)
        pos = 61

    whitelisted: bool = False
    integrator_fee: uint256 = 0
    share: uint256 = 0
    resolver_fee: uint256 = 0
    whitelisted, integrator_fee, share, resolver_fee, pos = self._parse_fee_data(
        _extra_data, pos, _taker, True
    )
    assert whitelisted or (
        self.access_token != empty(address)
        and staticcall AccessToken(self.access_token).balanceOf(_taker) > 0
    ), OnlyWhitelistOrAccessToken()
    denominator: uint256 = BASE_1E5 + integrator_fee + resolver_fee
    integrator_total: uint256 = _taking_amount * integrator_fee // denominator
    integrator_amount: uint256 = integrator_total * share // BASE_1E2
    protocol_amount: uint256 = (
        _taking_amount * resolver_fee // denominator + integrator_total - integrator_amount
    )
    if self.fusion:
        estimate: uint256 = convert(slice(_extra_data, pos, 32), uint256)
        assert estimate >= _order.takingAmount, InvalidEstimatedTakingAmount()
        actual: uint256 = _taking_amount - integrator_amount - protocol_amount
        scaled_estimate: uint256 = self._ceil_div(estimate, _making_amount, _order.makingAmount)
        if actual > scaled_estimate:
            surplus_fee: uint256 = convert(slice(_extra_data, pos + 32, 1), uint256)
            assert surplus_fee <= BASE_1E2, InvalidProtocolSurplusFee()
            protocol_amount += (actual - scaled_estimate) * surplus_fee // BASE_1E2
        pos += 33
    assert len(_extra_data) - pos < 20, NotModelled()

    taker_asset: IERC20 = IERC20(convert(_order.takerAsset, address))
    if convert(_order.receiver, address) == self:
        if integrator_amount > 0:
            assert extcall taker_asset.transfer(
                integrator, integrator_amount, default_return_value=True
            )
        if protocol_amount > 0:
            assert extcall taker_asset.transfer(
                protocol, protocol_amount, default_return_value=True
            )
        assert extcall taker_asset.transfer(
            receiver,
            _taking_amount - integrator_amount - protocol_amount,
            default_return_value=True,
        )
    else:
        assert integrator_amount + protocol_amount == 0, InconsistentFee()
