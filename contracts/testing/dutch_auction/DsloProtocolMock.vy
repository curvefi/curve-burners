# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DsloProtocolMock
@author Curve Finance
@license MIT
@notice Minimal KyberSwap DSLOProtocol stand-in for the Kyber adapter
        deployment and settlement tests: DOMAIN_SEPARATOR and hashOrder over
        a configurable domain, the linear amount calculators, the nonce
        manager and the timestamp predicate, and a fillOrderTo that follows
        DSOrderMixin.fillOrderTo for what the adapter relies on — the
        contract signature checked on the first fill only, the predicate
        (`and` is a Vyper keyword, so the mock dispatches it internally and
        staticcalls the entries like PredicateHelper does), amounts through
        the getter bytes (exact fills without them), the fee branches, the
        remaining bookkeeping, and the transfers. Not modelled: the operator
        co-signature, interactions, taker callbacks, pausing. Settlement
        against the real protocol is the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.kyber import dslo
from contracts.utils import constants as c


struct AmountData:
    makingAmount: uint256
    takingAmount: uint256
    thresholdAmount: uint256


struct Signature:
    orderSignature: Bytes[SIGNATURE_BOUND]
    opSignature: Bytes[65]


struct FillOrderParams:
    order: dslo.Order
    signature: Signature
    opExpireTime: uint32
    target: address
    callbackData: Bytes[32]


error BadSignature:
    pass


error InvalidOrder:
    pass


error PredicateFailed:
    pass


error WrongAmount:
    pass


error BadAmounts:
    pass


SIGNATURE_BOUND: constant(uint256) = 20 + dslo.ENCODED_ORDER_BOUND
ORDER_FILLED: constant(uint256) = 1

domain: immutable(bytes32)

# NonceManager
nonce: public(HashMap[address, uint256])
# DSOrderMixin._remaining: 0 for an untouched order, 1 for a filled or
# cancelled one, else remaining + 1.
remainingRaw: public(HashMap[bytes32, uint256])


@deploy
def __init__(_domain_override: bytes32):
    separator: bytes32 = _domain_override
    if separator == empty(bytes32):
        separator = dslo._domain_separator(chain.id, self)
    self.domain = separator


# EIP-712


@external
@view
def DOMAIN_SEPARATOR() -> bytes32:
    return self.domain


@external
@pure
def LIMIT_ORDER_TYPEHASH() -> bytes32:
    return dslo.ORDER_TYPE_HASH


@external
@view
def hashOrder(_order: dslo.Order) -> bytes32:
    return dslo._order_digest(_order, self.domain)


# AmountCalculator


@external
@pure
def getMakerAmount(
    _order_maker_amount: uint256, _order_taker_amount: uint256, _swap_taker_amount: uint256
) -> uint256:
    return _swap_taker_amount * _order_maker_amount // _order_taker_amount


@external
@pure
def getTakerAmount(
    _order_maker_amount: uint256, _order_taker_amount: uint256, _swap_maker_amount: uint256
) -> uint256:
    return (
        _swap_maker_amount * _order_taker_amount + _order_maker_amount - 1
    ) // _order_maker_amount


# NonceManager and PredicateHelper


@external
def increaseNonce():
    self.nonce[msg.sender] += 1


@external
@view
def nonceEquals(_maker: address, _nonce: uint256) -> bool:
    return self.nonce[_maker] == _nonce


@external
@view
def timestampBelow(_time: uint256) -> bool:
    return block.timestamp < _time


@internal
@view
def _static_bool(_target: address, _data: Bytes[dslo.MAX_PREDICATE]) -> bool:
    result: Bytes[32] = raw_call(_target, _data, max_outsize=32, is_static_call=True)
    assert len(result) == 32, PredicateFailed()
    return convert(result, uint256) != 0


@internal
@view
def _check_predicate(_predicate: Bytes[dslo.MAX_PREDICATE]) -> bool:
    if convert(slice(_predicate, 0, 4), bytes4) != dslo.AND_SELECTOR:
        return self._static_bool(self, _predicate)
    targets: DynArray[address, dslo.MAX_PREDICATES] = []
    data: DynArray[Bytes[dslo.MAX_PREDICATE_CALL], dslo.MAX_PREDICATES] = []
    targets, data = abi_decode(
        slice(_predicate, 4, len(_predicate) - 4),
        (
            DynArray[address, dslo.MAX_PREDICATES],
            DynArray[Bytes[dslo.MAX_PREDICATE_CALL], dslo.MAX_PREDICATES],
        ),
    )
    for i: uint256 in range(dslo.MAX_PREDICATES):
        if i >= len(targets):
            break
        if not self._static_bool(targets[i], data[i]):
            return False
    return True


# Orders


@external
def cancelOrder(_order: dslo.Order):
    assert _order.maker == msg.sender
    self.remainingRaw[dslo._order_digest(_order, self.domain)] = ORDER_FILLED


@internal
@view
def _call_getter(
    _getter: Bytes[dslo.GETTER_LEN],
    _order_expected_amount: uint256,
    _amount: uint256,
    _order_result_amount: uint256,
) -> uint256:
    if len(_getter) == 0:
        assert _amount == _order_expected_amount, WrongAmount()
        return _order_result_amount
    result: Bytes[32] = raw_call(
        self, concat(_getter, abi_encode(_amount)), max_outsize=32, is_static_call=True
    )
    assert len(result) == 32, WrongAmount()
    return convert(result, uint256)


@external
def fillOrderTo(
    _params: FillOrderParams, _amounts: AmountData, _sender: address
) -> (uint256, uint256, uint256):
    """
    @notice DSOrderMixin.fillOrderTo for the modelled paths; the operator
            co-signature is not checked.
    """
    order: dslo.Order = _params.order
    order_hash: bytes32 = dslo._order_digest(order, self.domain)
    remaining: uint256 = self.remainingRaw[order_hash]
    assert remaining != ORDER_FILLED, InvalidOrder()
    assert order.allowedSender == empty(address) or order.allowedSender == msg.sender
    if remaining == 0:
        # First fill: the contract signature, verbatim bytes, the digest.
        success: bool = False
        response: Bytes[32] = b""
        success, response = raw_call(
            order.maker,
            abi_encode(
                order_hash,
                _params.signature.orderSignature,
                method_id=method_id("isValidSignature(bytes32,bytes)"),
            ),
            max_outsize=32,
            is_static_call=True,
            revert_on_failure=False,
        )
        assert (
            success
            and len(response) == 32
            and convert(slice(response, 0, 4), bytes4) == c.ERC1271_MAGIC_VALUE
        ), BadSignature()
        remaining = order.makingAmount
    else:
        remaining -= 1
    if len(order.predicate) > 0:
        assert self._check_predicate(order.predicate), PredicateFailed()

    amounts: AmountData = _amounts
    assert (amounts.takingAmount == 0) != (amounts.makingAmount == 0), BadAmounts()
    if amounts.takingAmount == 0:
        requested: uint256 = amounts.makingAmount
        amounts.makingAmount = min(amounts.makingAmount, remaining)
        amounts.takingAmount = self._call_getter(
            order.getTakerAmount, order.makingAmount, amounts.makingAmount, order.takingAmount
        )
        assert amounts.takingAmount * requested <= amounts.thresholdAmount * amounts.makingAmount
    else:
        requested: uint256 = amounts.takingAmount
        amounts.makingAmount = self._call_getter(
            order.getMakerAmount, order.takingAmount, amounts.takingAmount, order.makingAmount
        )
        if amounts.makingAmount > remaining:
            amounts.makingAmount = remaining
            amounts.takingAmount = self._call_getter(
                order.getTakerAmount, order.makingAmount, amounts.makingAmount, order.takingAmount
            )
        assert amounts.makingAmount * requested >= amounts.thresholdAmount * amounts.takingAmount
    assert amounts.makingAmount > 0 and amounts.takingAmount > 0, BadAmounts()
    self.remainingRaw[order_hash] = remaining - amounts.makingAmount + 1

    fee_recipient: address = empty(address)
    fee_percent: uint256 = 0
    taker_asset_fee: bool = False
    fee_recipient, fee_percent, taker_asset_fee = dslo._fee_config(order.feeConfig)
    fee: uint256 = 0
    maker_asset: IERC20 = IERC20(order.makerAsset)
    taker_asset: IERC20 = IERC20(order.takerAsset)
    if fee_recipient != empty(address) and fee_percent != 0:
        if taker_asset_fee:
            fee = (amounts.takingAmount * fee_percent + dslo.FEE_UNITS - 1) // dslo.FEE_UNITS
            assert extcall maker_asset.transferFrom(
                order.maker, _params.target, amounts.makingAmount, default_return_value=True
            )
            assert extcall taker_asset.transferFrom(
                msg.sender, fee_recipient, fee, default_return_value=True
            )
        else:
            fee = (amounts.makingAmount * fee_percent + dslo.FEE_UNITS - 1) // dslo.FEE_UNITS
            assert extcall maker_asset.transferFrom(
                order.maker, fee_recipient, fee, default_return_value=True
            )
            assert extcall maker_asset.transferFrom(
                order.maker, _params.target, amounts.makingAmount - fee, default_return_value=True
            )
    else:
        assert extcall maker_asset.transferFrom(
            order.maker, _params.target, amounts.makingAmount, default_return_value=True
        )
    assert extcall taker_asset.transferFrom(
        msg.sender, dslo._receiver(order), amounts.takingAmount, default_return_value=True
    )
    if taker_asset_fee:
        return amounts.makingAmount, amounts.takingAmount + fee, fee
    return amounts.makingAmount, amounts.takingAmount, fee
