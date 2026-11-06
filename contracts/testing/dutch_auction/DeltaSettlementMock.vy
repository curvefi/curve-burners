# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DeltaSettlementMock
@author Curve Finance
@license MIT
@notice Minimal Velora Delta (Portikus V2) stand-in for the Delta adapter
        deployment and settlement tests: DOMAIN_SEPARATOR over a
        configurable domain, swapSettle and swapSettleFillable following
        SwapSettlementModule / FillableSwapSettlementModule v2.3.0 and the
        token flows the adapter relies on — the agent allow-list, the
        contract signature checked on every fill (or a pre-signature),
        the unordered nonce bitmap, deadline and kind, the sell token
        pulled from the owner to the agent's executor by plain
        transferFrom, the executor called, the beneficiary paid the
        received amount after FeeManagerLib.processSellFees. Not modelled:
        EOA signatures, permits and Permit2, native coin, bridges,
        cosignatures, batches, buy orders. Settlement against the real
        protocol is the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.velora import delta
from contracts.utils import constants as c


interface Executor:
    def execute(_data: Bytes[MAX_EXECUTOR_DATA]): nonpayable


struct BridgeOverride:
    protocolSelector: bytes4
    protocolData: Bytes[delta.MAX_PROTOCOL_DATA]


struct OrderWithSig:
    order: delta.Order
    signature: Bytes[SIGNATURE_BOUND]
    bridgeOverride: BridgeOverride
    cosignature: Bytes[65]


error UnauthorizedAgent:
    pass


error InvalidSignature:
    pass


error NotPreSigned:
    pass


error InvalidNonce:
    pass


error DeadlineExpired:
    pass


error InvalidOrderType:
    pass


error UnsupportedPermit:
    pass


error InsufficientReturnAmount:
    pass


error InvalidFillAmount:
    pass


MAX_EXECUTOR_DATA: constant(uint256) = 1024
SIGNATURE_BOUND: constant(uint256) = 20 + delta.ENCODED_ORDER_BOUND

# The separator every digest uses: derived from this chain and address like
# the protocol, or an override that makes the adapter's deploy-time
# cross-check fail.
domain: immutable(bytes32)

# Registry.isAgentRegistered.
agents: public(HashMap[address, bool])
# NonceManagerLib: one bit per nonce and owner.
nonces: HashMap[address, HashMap[uint256, uint256]]
# SignatureLib.preSignatures.
pre_signatures: HashMap[address, HashMap[bytes32, bool]]
# FillableStorageLib.filledSell.
filled_sell: HashMap[bytes32, uint256]
# FeeManagerLib.fees, kept on this contract like the protocol does.
fees: public(HashMap[address, HashMap[address, uint256]])


@deploy
def __init__(_domain_override: bytes32):
    separator: bytes32 = _domain_override
    if separator == empty(bytes32):
        separator = delta._domain_separator(chain.id, self)
    self.domain = separator


# Protocol views and configuration


@external
@view
def DOMAIN_SEPARATOR() -> bytes32:
    return self.domain


@external
def set_agent(_agent: address, _registered: bool):
    self.agents[_agent] = _registered


@external
@view
def isNonceUsed(_owner: address, _nonce: uint256) -> bool:
    return self.nonces[_owner][_nonce >> 8] & (1 << (_nonce % 256)) != 0


@external
def invalidateNonce(_nonce: uint256):
    self.nonces[msg.sender][_nonce >> 8] |= 1 << (_nonce % 256)


@external
def setPreSignature(_order_hash: bytes32, _pre_signed: bool):
    self.pre_signatures[msg.sender][_order_hash] = _pre_signed


@external
@view
def swapFilledAmount(_order: delta.Order) -> uint256:
    return self.filled_sell[delta._order_digest(_order, self.domain, True)]


# Settlement internals


@internal
@view
def _verify_signature(_order_hash: bytes32, _owner: address, _signature: Bytes[SIGNATURE_BOUND]):
    """
    @notice SignatureLib.verify for a contract owner: a revert of the
            owner's isValidSignature bubbles up unchanged with the callee's
            data, a wrong answer is InvalidSignature.
    """
    if len(_signature) == 0:
        assert self.pre_signatures[_owner][_order_hash], NotPreSigned()
        return
    assert _owner.is_contract, InvalidSignature()
    response: Bytes[32] = raw_call(
        _owner,
        abi_encode(_order_hash, _signature, method_id=method_id("isValidSignature(bytes32,bytes)")),
        max_outsize=32,
        is_static_call=True,
    )
    assert len(response) == 32 and (
        convert(slice(response, 0, 4), bytes4) == c.ERC1271_MAGIC_VALUE
    ), InvalidSignature()


@internal
def _use_nonce(_owner: address, _nonce: uint256):
    slot: uint256 = _nonce >> 8
    bit: uint256 = 1 << (_nonce % 256)
    assert self.nonces[_owner][slot] & bit == 0, InvalidNonce()
    self.nonces[_owner][slot] |= bit


@internal
def _process_sell_fees(
    _partner_and_fee: uint256, _token: address, _received: uint256, _expected: uint256
) -> uint256:
    """
    @notice FeeManagerLib.processSellFees: the amount left for the
            beneficiary after the protocol's and the partner's cuts.
    """
    partner: address = empty(address)
    fee_bps: uint256 = 0
    takes_surplus: bool = False
    cap_surplus: bool = False
    partner, fee_bps, takes_surplus, cap_surplus = delta._parse_partner_and_fee(_partner_and_fee)
    return_amount: uint256 = _received
    if _received > _expected:
        surplus: uint256 = _received - _expected
        if cap_surplus:
            surplus = min(surplus, _expected * 100 // delta.HUNDRED_PERCENT)
        protocol_fee: uint256 = surplus * 5000 // delta.HUNDRED_PERCENT
        self.fees[empty(address)][_token] += protocol_fee
        return_amount -= protocol_fee
        if partner != empty(address):
            partner_fee: uint256 = 0
            if fee_bps != 0:
                partner_fee = return_amount * fee_bps // delta.HUNDRED_PERCENT
            elif takes_surplus:
                partner_fee = surplus - protocol_fee
            self.fees[partner][_token] += partner_fee
            return_amount -= partner_fee
    elif partner != empty(address) and fee_bps > 0:
        partner_fee: uint256 = return_amount * fee_bps // delta.HUNDRED_PERCENT
        self.fees[partner][_token] += partner_fee
        return_amount -= partner_fee
    return return_amount


@internal
def _fill(
    _ows: OrderWithSig,
    _executor_data: Bytes[MAX_EXECUTOR_DATA],
    _executor: address,
    _amount_in: uint256,
    _min_out: uint256,
    _expected_out: uint256,
) -> uint256:
    """
    @notice The shared body of _pre/_execute/_post: pull the sell amount
            from the owner to the executor, run it, require the minimum
            on this contract and pay the beneficiary after fees.
    """
    order: delta.Order = _ows.order
    assert block.timestamp <= order.deadline, DeadlineExpired()
    assert order.kind == delta.KIND_SELL, InvalidOrderType()
    assert len(order.permit) == 0, UnsupportedPermit()
    dest: IERC20 = IERC20(order.destToken)
    balance_before: uint256 = staticcall dest.balanceOf(self)
    assert extcall IERC20(order.srcToken).transferFrom(
        order.owner, _executor, _amount_in, default_return_value=True
    )
    extcall Executor(_executor).execute(_executor_data)
    received: uint256 = (staticcall dest.balanceOf(self)) - balance_before
    assert received >= _min_out, InsufficientReturnAmount()
    beneficiary: address = order.beneficiary
    if beneficiary == empty(address):
        beneficiary = order.owner
    return_amount: uint256 = self._process_sell_fees(
        order.partnerAndFee, order.destToken, received, _expected_out
    )
    assert extcall dest.transfer(beneficiary, return_amount, default_return_value=True)
    return return_amount


# Settlement entry points


@external
@payable
def swapSettle(
    _ows: OrderWithSig,
    _executor_data: Bytes[MAX_EXECUTOR_DATA],
    _executor: address,
    _bridge_data: Bytes[256],
) -> uint256:
    """
    @notice SwapSettlementModule.swapSettle: fill-or-kill under the Order
            primary type; the nonce is burnt up front.
    @return Amount paid to the beneficiary.
    """
    assert self.agents[msg.sender], UnauthorizedAgent()
    order: delta.Order = _ows.order
    self._use_nonce(order.owner, order.nonce)
    order_hash: bytes32 = delta._order_digest(order, self.domain, False)
    self._verify_signature(order_hash, order.owner, _ows.signature)
    return self._fill(
        _ows, _executor_data, _executor, order.srcAmount, order.destAmount, order.expectedAmount
    )


@external
@payable
def swapSettleFillable(
    _ows: OrderWithSig,
    _executor_data: Bytes[MAX_EXECUTOR_DATA],
    _executor: address,
    _fill_percent: uint256,
    _bridge_data: Bytes[256],
) -> uint256:
    """
    @notice FillableSwapSettlementModule.swapSettleFillable: a pro-rata
            slice under the FillableOrder primary type; the signature is
            checked on every slice and the nonce is burnt by the last one.
    @return Amount paid to the beneficiary.
    """
    assert self.agents[msg.sender], UnauthorizedAgent()
    order: delta.Order = _ows.order
    amount_in: uint256 = order.srcAmount * _fill_percent // delta.HUNDRED_PERCENT
    min_out: uint256 = order.destAmount * _fill_percent // delta.HUNDRED_PERCENT
    expected_out: uint256 = order.expectedAmount * _fill_percent // delta.HUNDRED_PERCENT
    assert amount_in != 0 and min_out != 0 and expected_out != 0, InvalidFillAmount()
    assert self.nonces[order.owner][order.nonce >> 8] & (1 << (order.nonce % 256)) == 0, (
        InvalidNonce()
    )
    order_hash: bytes32 = delta._order_digest(order, self.domain, True)
    self._verify_signature(order_hash, order.owner, _ows.signature)
    paid: uint256 = self._fill(_ows, _executor_data, _executor, amount_in, min_out, expected_out)
    total_filled: uint256 = self.filled_sell[order_hash] + amount_in
    self.filled_sell[order_hash] = total_filled
    assert total_filled <= order.srcAmount, InvalidFillAmount()
    if total_filled == order.srcAmount:
        self.nonces[order.owner][order.nonce >> 8] |= 1 << (order.nonce % 256)
    return paid
