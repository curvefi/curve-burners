# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title JamSettlementMock
@author Curve Finance
@license MIT
@notice Minimal Bebop JamSettlement stand-in for the JAM adapter deployment
        and settlement tests: DOMAIN_SEPARATOR, balanceManager and
        hashJamOrder over a configurable domain, and settle/settleInternal
        following JamValidation.validateOrder and the token flows the
        adapter relies on — the contract signature checked on every fill
        unless the taker is the caller, nonce burning, executor
        exclusivity, strict expiry, the sell tokens pulled through the
        balance manager, the receiver paid at least the signed buy amount
        after partner and protocol fees. Not modelled: Permit2, native
        coin, hooks beyond their hash, batches, Blend orders. Settlement
        against the real protocol is the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.bebop import jam
from contracts.utils import constants as c


interface BalanceManager:
    def transferTokens(
        _tokens: DynArray[address, jam.MAX_TOKENS],
        _amounts: DynArray[uint256, jam.MAX_TOKENS],
        _sender: address,
        _receiver: address,
    ): nonpayable


struct Interaction:
    result: bool
    to: address
    value: uint256
    data: Bytes[1024]


error InvalidContractSignature:
    pass


error InvalidExecutor:
    pass


error OrderExpired:
    pass


error ZeroNonce:
    pass


error InvalidNonce:
    pass


error SellTokensInvalidLength:
    pass


error BuyTokensInvalidLength:
    pass


error InvalidOutputBalance:
    token: address
    expected: uint256
    actual: uint256


error InvalidFilledAmounts:
    expected: uint256
    actual: uint256


error InteractionsFailed:
    pass


MAX_INTERACTIONS: constant(uint256) = 8
MAX_HOOKS_DATA: constant(uint256) = 1024
SIGNATURE_BOUND: constant(uint256) = 20 + jam.ENCODED_ORDER_BOUND

# The separator every digest uses: derived from this chain and address like
# the protocol, or an override that makes the adapter's deploy-time
# cross-check fail.
domain: immutable(bytes32)
balance_manager: public(address)
protocol_fee_address: public(immutable(address))

# JamValidation.standardNonces: one bit per nonce and taker.
nonces: HashMap[address, HashMap[uint256, uint256]]


@deploy
def __init__(_domain_override: bytes32, _protocol_fee_address: address):
    separator: bytes32 = _domain_override
    if separator == empty(bytes32):
        separator = jam._domain_separator(chain.id, self)
    self.domain = separator
    self.protocol_fee_address = _protocol_fee_address


@external
def set_balance_manager(_balance_manager: address):
    # The real settlement creates its balance manager in the constructor;
    # the mock is wired after both are deployed.
    self.balance_manager = _balance_manager


@external
@view
def DOMAIN_SEPARATOR() -> bytes32:
    return self.domain


@external
@view
def balanceManager() -> address:
    return self.balance_manager


@internal
@pure
def _signed(_order: jam.JamOrderCalldata, _hooks_hash: bytes32) -> jam.JamOrder:
    return jam.JamOrder(
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
        hooksHash=_hooks_hash,
    )


@external
@view
def hashJamOrder(_order: jam.JamOrderCalldata, _hooks_hash: bytes32) -> bytes32:
    return jam._order_digest(self._signed(_order, _hooks_hash), self.domain)


@internal
@view
def _hooks_hash(_hooks_data: Bytes[MAX_HOOKS_DATA]) -> bytes32:
    # JamHooks.hash over the decoded hooks; the mock hashes the bytes as
    # given (canonical encoding assumed) and treats empty data as no hooks.
    if len(_hooks_data) == 0:
        return jam.EMPTY_HOOKS_HASH
    return keccak256(_hooks_data)


@internal
def _validate_order(
    _order: jam.JamOrderCalldata, _signature: Bytes[SIGNATURE_BOUND], _hooks_hash: bytes32
):
    """
    @notice JamValidation.validateOrder for orders without Permit2.
    """
    if _order.taker != msg.sender:
        order_hash: bytes32 = jam._order_digest(self._signed(_order, _hooks_hash), self.domain)
        success: bool = False
        response: Bytes[32] = b""
        # IERC1271(taker).isValidSignature: a revert bubbles up unchanged
        # with the callee's data, a wrong answer is InvalidContractSignature.
        response = raw_call(
            _order.taker,
            abi_encode(
                order_hash, _signature, method_id=method_id("isValidSignature(bytes32,bytes)")
            ),
            max_outsize=32,
            is_static_call=True,
        )
        assert len(response) == 32 and (
            convert(slice(response, 0, 4), bytes4) == c.ERC1271_MAGIC_VALUE
        ), InvalidContractSignature()
    assert _order.nonce != 0, ZeroNonce()
    slot: uint256 = _order.nonce >> 8
    bit: uint256 = 1 << (_order.nonce % 256)
    assert self.nonces[_order.taker][slot] & bit == 0, InvalidNonce()
    self.nonces[_order.taker][slot] |= bit
    assert (
        _order.executor == msg.sender
        or _order.executor == empty(address)
        or block.timestamp > _order.exclusivityDeadline
    ), InvalidExecutor()
    assert len(_order.buyTokens) == len(_order.buyAmounts), BuyTokensInvalidLength()
    assert len(_order.sellTokens) == len(_order.sellAmounts), SellTokensInvalidLength()
    assert block.timestamp < _order.expiry, OrderExpired()


@internal
def _distribute_fees(_partner_info: uint256, _token: IERC20, _amount: uint256) -> uint256:
    # JamPartner.distributeFees: fees are taken from the amount in hand.
    partner: address = empty(address)
    partner_fee: uint256 = 0
    protocol_fee: uint256 = 0
    partner, partner_fee, protocol_fee = jam._unpack_partner_info(_partner_info)
    sent: uint256 = 0
    if partner_fee > 0:
        fee: uint256 = _amount * partner_fee // jam.HUNDRED_PERCENT_BPS
        assert extcall _token.transfer(partner, fee, default_return_value=True)
        sent += fee
    if protocol_fee > 0:
        fee: uint256 = _amount * protocol_fee // jam.HUNDRED_PERCENT_BPS
        assert extcall _token.transfer(self.protocol_fee_address, fee, default_return_value=True)
        sent += fee
    return sent


@external
@view
def isNonceValid(_taker: address, _nonce: uint256) -> bool:
    return self.nonces[_taker][_nonce >> 8] & (1 << (_nonce % 256)) == 0


@external
@payable
def settle(
    _order: jam.JamOrderCalldata,
    _signature: Bytes[SIGNATURE_BOUND],
    _interactions: DynArray[Interaction, MAX_INTERACTIONS],
    _hooks_data: Bytes[MAX_HOOKS_DATA],
    _balance_recipient: address,
):
    """
    @notice JamSettlement.settle for the modelled paths: the solver's
            interactions must leave the buy tokens on this contract, whose
            whole balance (less fees) goes to the receiver.
    """
    self._validate_order(_order, _signature, self._hooks_hash(_hooks_data))
    extcall BalanceManager(self.balance_manager).transferTokens(
        _order.sellTokens, _order.sellAmounts, _order.taker, _balance_recipient
    )
    for interaction: Interaction in _interactions:
        success: bool = raw_call(
            interaction.to, interaction.data, value=interaction.value, revert_on_failure=False
        )
        assert success or not interaction.result, InteractionsFailed()
    for i: uint256 in range(len(_order.buyTokens), bound=jam.MAX_TOKENS):
        token: IERC20 = IERC20(_order.buyTokens[i])
        amount: uint256 = staticcall token.balanceOf(self)
        if _order.partnerInfo != 0:
            amount -= self._distribute_fees(_order.partnerInfo, token, amount)
        assert amount >= _order.buyAmounts[i], (
            InvalidOutputBalance(token=token.address, expected=_order.buyAmounts[i], actual=amount)
        )
        assert extcall token.transfer(_order.receiver, amount, default_return_value=True)


@external
@payable
def settleInternal(
    _order: jam.JamOrderCalldata,
    _signature: Bytes[SIGNATURE_BOUND],
    _filled_amounts: DynArray[uint256, jam.MAX_TOKENS],
    _hooks_data: Bytes[MAX_HOOKS_DATA],
):
    """
    @notice JamSettlement.settleInternal: the caller pays from its own
            balance through the balance manager, at least the signed buy
            amounts after fees.
    """
    self._validate_order(_order, _signature, self._hooks_hash(_hooks_data))
    extcall BalanceManager(self.balance_manager).transferTokens(
        _order.sellTokens, _order.sellAmounts, _order.taker, msg.sender
    )
    amounts: DynArray[uint256, jam.MAX_TOKENS] = _order.buyAmounts
    if len(_filled_amounts) != 0:
        amounts = _filled_amounts
    partner: address = empty(address)
    partner_fee: uint256 = 0
    protocol_fee: uint256 = 0
    if _order.partnerInfo != 0:
        partner, partner_fee, protocol_fee = jam._unpack_partner_info(_order.partnerInfo)
    for i: uint256 in range(len(amounts), bound=jam.MAX_TOKENS):
        partner_cut: uint256 = amounts[i] * partner_fee // jam.HUNDRED_PERCENT_BPS
        protocol_cut: uint256 = amounts[i] * protocol_fee // jam.HUNDRED_PERCENT_BPS
        net: uint256 = amounts[i] - partner_cut - protocol_cut
        assert net >= _order.buyAmounts[i], (
            InvalidFilledAmounts(expected=_order.buyAmounts[i], actual=net)
        )
        extcall BalanceManager(self.balance_manager).transferTokens(
            [_order.buyTokens[i]], [net], msg.sender, _order.receiver
        )
        if partner_cut != 0:
            extcall BalanceManager(self.balance_manager).transferTokens(
                [_order.buyTokens[i]], [partner_cut], msg.sender, partner
            )
        if protocol_cut != 0:
            extcall BalanceManager(self.balance_manager).transferTokens(
                [_order.buyTokens[i]], [protocol_cut], msg.sender, self.protocol_fee_address
            )
