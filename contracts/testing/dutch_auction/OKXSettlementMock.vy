# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OKXSettlementMock
@author Curve Finance
@license MIT
@notice Minimal OKX Intent Swap Settlement stand-in for the OKX adapter
        deployment and settlement tests: domainSeparator and
        tokenApproveProxy over a configurable domain, and settle following
        the legacy calldata schema and the checks the adapter relies on —
        the solver allowlist, the trade flags (exactIn only, no partial
        fills, the EIP-1271 scheme with the owner prefixed to the
        signature), the contract signature checked on every fill, the owner
        match, expiry, the limit price against the clearing prices, the
        fill-or-kill uid accounting, the sold tokens pulled through the
        approval proxy, commissions and the solver fee in both directions
        and fund flows, mid-settlement interactions, and the receiver paid
        at least the signed toTokenAmount after fees. Not modelled: ECDSA
        and pre-sign schemes, the surplus distribution, the threshold
        schema, native-coin payouts. Settlement against the real protocol is
        the fork test's job.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.okx import intent_swap
from contracts.utils import constants as c


interface TokenApproveProxy:
    def claimTokens(_token: address, _who: address, _dest: address, _amount: uint256): nonpayable


struct SolverFeeInfo:
    feePercent: uint256
    solverAddr: address
    flag: uint256


# TradeLib.Data of the legacy schema (no thresholdPercent).
struct Trade:
    fromTokenAddressIndex: uint256
    toTokenAddressIndex: uint256
    owner: address
    receiver: address
    fromTokenAmount: uint256
    toTokenAmount: uint256
    validTo: uint32
    appData: bytes32
    flags: uint256
    executedAmount: uint256
    commissionInfos: DynArray[intent_swap.CommissionInfo, intent_swap.MAX_COMMISSIONS]
    signature: Bytes[SIGNATURE_BOUND]
    solverFeeInfo: SolverFeeInfo


struct Interaction:
    target: address
    value: uint256
    callData: Bytes[MAX_CALLDATA]


struct SurplusFeeInfo:
    feePercent: uint256
    trimReceiver: address
    flag: uint256


struct Payout:
    account: address
    token: address
    amount: uint256


error NotASolver:
    pass


error OnlyExactIn:
    pass


error PartialFillNotSupported:
    pass


error SchemeNotModelled:
    pass


error InvalidEip1271Signature:
    pass


error OwnerMismatch:
    pass


error OrderExpired:
    pass


error SameTokenSwap:
    pass


error ZeroClearingPrice:
    pass


error LimitPriceNotRespected:
    pass


error OrderInvalidated:
    pass


error OrderFilled:
    pass


error ToTokenAmountNotRespected:
    pass


error ForbiddenInteraction:
    pass


error InteractionFailed:
    pass


MAX_TOKENS: constant(uint256) = 8
MAX_TRADES: constant(uint256) = 4
MAX_INTERACTIONS: constant(uint256) = 8
MAX_CALLDATA: constant(uint256) = 1024
MAX_PAYOUTS: constant(uint256) = MAX_TRADES * (2 + intent_swap.MAX_COMMISSIONS)
# owner ++ adapter ++ abi_encode(Order).
SIGNATURE_BOUND: constant(uint256) = 20 + 20 + intent_swap.ENCODED_ORDER_BOUND
SWAP_MODE_BIT: constant(uint256) = 1
PARTIAL_FILL_BIT: constant(uint256) = 2

# The separator every digest uses: derived from this chain and address like
# the protocol, or an override that makes the adapter's deploy-time
# cross-check fail.
domain: immutable(bytes32)
tokenApproveProxy: public(immutable(address))
solvers: HashMap[address, bool]
# filledAmount keyed by the uid hash (a bytes key stands in for the
# protocol's mapping(bytes => uint256)); max_value marks an invalidated uid.
filled: HashMap[bytes32, uint256]


@deploy
def __init__(_domain_override: bytes32, _token_approve_proxy: address):
    separator: bytes32 = _domain_override
    if separator == empty(bytes32):
        separator = intent_swap._domain_separator(chain.id, self)
    self.domain = separator
    self.tokenApproveProxy = _token_approve_proxy


@external
def set_solver(_solver: address, _allowed: bool):
    self.solvers[_solver] = _allowed


@external
@view
def isSolver(_solver: address) -> bool:
    return self.solvers[_solver]


@external
@view
def domainSeparator() -> bytes32:
    return self.domain


@external
@view
def filledAmount(_uid: Bytes[intent_swap.UID_LENGTH]) -> uint256:
    return self.filled[keccak256(_uid)]


@external
def invalidateOrder(_uid: Bytes[intent_swap.UID_LENGTH]):
    assert convert(slice(_uid, 32, 20), address) == msg.sender, OwnerMismatch()
    self.filled[keccak256(_uid)] = max_value(uint256)


# Settlement


@internal
@view
def _recover_order(
    _trade: Trade, _tokens: DynArray[address, MAX_TOKENS]
) -> (intent_swap.Order, bytes32):
    """
    @notice TradeLib.extractOrder, Signing.recoverOrderFromTrade and
            recoverEip1271Signer for the EIP-1271 scheme.
    """
    assert _trade.flags & SWAP_MODE_BIT == 0, OnlyExactIn()
    assert _trade.flags & PARTIAL_FILL_BIT == 0, PartialFillNotSupported()
    assert _trade.flags >> intent_swap.SIGNING_SCHEME_OFFSET == intent_swap.EIP1271_SCHEME, (
        SchemeNotModelled()
    )
    order: intent_swap.Order = intent_swap.Order(
        fromTokenAddress=_tokens[_trade.fromTokenAddressIndex],
        toTokenAddress=_tokens[_trade.toTokenAddressIndex],
        owner=_trade.owner,
        receiver=_trade.receiver,
        fromTokenAmount=_trade.fromTokenAmount,
        toTokenAmount=_trade.toTokenAmount,
        validTo=_trade.validTo,
        appData=_trade.appData,
        swapMode=intent_swap.EXACT_IN,
        partiallyFillable=False,
        commissionInfos=_trade.commissionInfos,
    )
    digest: bytes32 = intent_swap._order_digest(order, self.domain)
    owner: address = convert(slice(_trade.signature, 0, 20), address)
    signature: Bytes[SIGNATURE_BOUND] = slice(_trade.signature, 20, len(_trade.signature) - 20)
    # EIP1271Verifier(owner).isValidSignature: a revert bubbles up unchanged
    # with the callee's data, a wrong answer is "invalid eip1271 signature".
    response: Bytes[32] = raw_call(
        owner,
        abi_encode(digest, signature, method_id=method_id("isValidSignature(bytes32,bytes)")),
        max_outsize=32,
        is_static_call=True,
    )
    assert (
        len(response) == 32 and convert(slice(response, 0, 4), bytes4) == c.ERC1271_MAGIC_VALUE
    ), (InvalidEip1271Signature())
    assert order.owner == owner, OwnerMismatch()
    return order, digest


@internal
def _process_trade(
    _trade: Trade,
    _tokens: DynArray[address, MAX_TOKENS],
    _prices: DynArray[uint256, MAX_TOKENS],
    _payouts: DynArray[Payout, MAX_PAYOUTS],
) -> DynArray[Payout, MAX_PAYOUTS]:
    """
    @notice Settlement._processSingleTrade and _executeFundPulls for one
            trade: validation, fee computation, the uid accounting, the pull
            from the owner (ToC fees straight to their recipients, the rest
            here) and the ToB from-token fees paid from this balance; the
            payouts are returned for the payout phase.
    """
    order: intent_swap.Order = empty(intent_swap.Order)
    digest: bytes32 = empty(bytes32)
    order, digest = self._recover_order(_trade, _tokens)
    assert convert(order.validTo, uint256) >= block.timestamp, OrderExpired()
    assert order.fromTokenAddress != order.toTokenAddress, SameTokenSwap()
    from_price: uint256 = _prices[_trade.fromTokenAddressIndex]
    to_price: uint256 = _prices[_trade.toTokenAddressIndex]
    assert from_price > 0 and to_price > 0, ZeroClearingPrice()
    assert order.fromTokenAmount * from_price >= order.toTokenAmount * to_price, (
        LimitPriceNotRespected()
    )

    # FeeLib: from-token fees come off the amount swapped, to-token fees off
    # the gross proceeds; ToC fees are pulled from the owner directly, ToB
    # fees are paid by the settlement.
    from_gross: uint256 = order.fromTokenAmount
    intent_swap._check_commissions(order.commissionInfos)
    solver: SolverFeeInfo = _trade.solverFeeInfo
    solver_is_from: bool = solver.feePercent > 0 and (
        solver.flag & intent_swap.FROM_TOKEN_COMMISSION != 0
        and solver.flag & intent_swap.TO_TOKEN_COMMISSION == 0
    )
    solver_is_to: bool = solver.feePercent > 0 and (
        solver.flag & intent_swap.TO_TOKEN_COMMISSION != 0
        and solver.flag & intent_swap.FROM_TOKEN_COMMISSION == 0
    )
    proxy: TokenApproveProxy = TokenApproveProxy(self.tokenApproveProxy)
    total_from_fee: uint256 = 0
    for info: intent_swap.CommissionInfo in order.commissionInfos:
        if info.flag & intent_swap.FROM_TOKEN_COMMISSION == 0 or info.feePercent == 0:
            continue
        fee: uint256 = from_gross * info.feePercent // intent_swap.COMMISSION_DENOM
        total_from_fee += fee
        if info.flag & intent_swap.IS_TO_B != 0:
            assert extcall IERC20(order.fromTokenAddress).transfer(
                info.referrerWalletAddress, fee, default_return_value=True
            )
        else:
            extcall proxy.claimTokens(
                order.fromTokenAddress, order.owner, info.referrerWalletAddress, fee
            )
    if solver_is_from:
        fee: uint256 = from_gross * solver.feePercent // intent_swap.COMMISSION_DENOM
        total_from_fee += fee
        extcall proxy.claimTokens(order.fromTokenAddress, order.owner, solver.solverAddr, fee)
    # The main pull: the gross less what went to ToC recipients directly.
    to_c_from_fee: uint256 = 0
    for info: intent_swap.CommissionInfo in order.commissionInfos:
        if (
            info.flag & intent_swap.FROM_TOKEN_COMMISSION != 0
            and info.flag & intent_swap.IS_TO_B == 0
        ):
            to_c_from_fee += from_gross * info.feePercent // intent_swap.COMMISSION_DENOM
    if solver_is_from:
        to_c_from_fee += from_gross * solver.feePercent // intent_swap.COMMISSION_DENOM
    extcall proxy.claimTokens(order.fromTokenAddress, order.owner, self, from_gross - to_c_from_fee)

    net_from: uint256 = from_gross - total_from_fee
    # Rounded up in the owner's favour like Math.mulDiv(..., Rounding.Up).
    to_gross: uint256 = (net_from * from_price + to_price - 1) // to_price
    payouts: DynArray[Payout, MAX_PAYOUTS] = _payouts
    total_to_fee: uint256 = 0
    for info: intent_swap.CommissionInfo in order.commissionInfos:
        if info.flag & intent_swap.TO_TOKEN_COMMISSION == 0 or info.feePercent == 0:
            continue
        fee: uint256 = to_gross * info.feePercent // intent_swap.COMMISSION_DENOM
        total_to_fee += fee
        payouts.append(
            Payout(account=info.referrerWalletAddress, token=order.toTokenAddress, amount=fee)
        )
    if solver_is_to:
        fee: uint256 = to_gross * solver.feePercent // intent_swap.COMMISSION_DENOM
        total_to_fee += fee
        payouts.append(Payout(account=solver.solverAddr, token=order.toTokenAddress, amount=fee))

    uid_hash: bytes32 = keccak256(intent_swap._order_uid(digest, order.owner, order.validTo))
    current: uint256 = self.filled[uid_hash]
    assert current != max_value(uint256), OrderInvalidated()
    assert current + from_gross <= order.fromTokenAmount, OrderFilled()
    self.filled[uid_hash] = current + from_gross

    assert to_gross - total_to_fee >= order.toTokenAmount, ToTokenAmountNotRespected()
    payouts.append(
        Payout(
            account=intent_swap._receiver(order),
            token=order.toTokenAddress,
            amount=to_gross - total_to_fee,
        )
    )
    return payouts


@internal
def _interact(_interactions: DynArray[Interaction, MAX_INTERACTIONS]):
    for interaction: Interaction in _interactions:
        assert interaction.target != self.tokenApproveProxy, ForbiddenInteraction()
        success: bool = raw_call(
            interaction.target,
            interaction.callData,
            value=interaction.value,
            revert_on_failure=False,
        )
        assert success, InteractionFailed()


@external
@payable
def settle(
    _settle_id: uint256,
    _tokens: DynArray[address, MAX_TOKENS],
    _clearing_prices: DynArray[uint256, MAX_TOKENS],
    _trades: DynArray[Trade, MAX_TRADES],
    _interactions: DynArray[Interaction, MAX_INTERACTIONS][3],
    _surplus_fee_info: SurplusFeeInfo,
):
    """
    @notice Settlement.settle for the modelled paths: pulls, mid
            interactions, then payouts from this balance.
    """
    assert self.solvers[msg.sender], NotASolver()
    assert len(_tokens) == len(_clearing_prices)  # dev: tokens/prices length mismatch
    self._interact(_interactions[0])
    payouts: DynArray[Payout, MAX_PAYOUTS] = []
    for trade: Trade in _trades:
        payouts = self._process_trade(trade, _tokens, _clearing_prices, payouts)
    self._interact(_interactions[1])
    for payout: Payout in payouts:
        if payout.amount == 0:
            continue
        assert extcall IERC20(payout.token).transfer(
            payout.account, payout.amount, default_return_value=True
        )
    self._interact(_interactions[2])
