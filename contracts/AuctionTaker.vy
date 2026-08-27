# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title AuctionTaker
@author Curve Finance
@license MIT
@notice Example filler for the Dutch auction's native take() rail: takes a lot,
        executes an arbitrary routing plan from the callback data (e.g. an
        aggregator swap of the lot into the want token), pays the auction's
        quote from the proceeds, and forwards the profit — all in one
        transaction, so a solver or agent only needs to find a route and
        submit it through this contract.
@dev The route is a bounded list of raw calls executed inside
     auctionTakeCallback, after the lot has been delivered and before the
     auction pulls the payment from this contract's want allowance. A typical
     plan is [approve(router, lot), router.swap(lot -> want)]. The route must
     leave at least the quoted payment in want on this contract; anything
     above it (in want or unspent lot tokens) is swept to the caller-chosen
     profit receiver and checked against the caller's minimum.
@custom:kill Nothing to kill: no owner, no configuration, and no funds or
             allowances at rest — every take clears its transient state and
             sweeps both tokens in the same transaction. Retire it by no
             longer routing fills through it.
@custom:security Permissionless by design: the contract must never custody
                 value between transactions, and nobody should grant it
                 allowances — route calls execute with this contract as
                 msg.sender, so any standing balance or allowance is claimable
                 by the first caller. The callback only accepts the auction
                 recorded transiently for the active take, and the
                 contract-wide reentrancy lock keeps route calls from
                 re-entering the take entrypoint.
"""


from ethereum.ercs import IERC20


error ZeroAuction:
    pass


error ZeroReceiver:
    pass


error OnlyTakenAuction:
    pass


error ProfitShortfall:
    pass


interface DutchAuction:
    def take(
        _from: address,
        maxAmount: uint256,
        takerReceiver: address,
        data: Bytes[MAX_CALLBACK_DATA],
    ) -> uint256: nonpayable
    def want() -> address: view


# One route step, executed via raw_call with this contract as the sender.
struct Call:
    target: address
    data: Bytes[MAX_CALL_DATA]


event RouteTaken:
    auction: indexed(address)
    token: indexed(address)
    caller: indexed(address)
    amount_taken: uint256
    payment: uint256
    profit: uint256


# Must match the auction core's callback data bound.
MAX_CALLBACK_DATA: constant(uint256) = 8192
# abi_encode(DynArray[Call, MAX_CALLS]) worst case is
# 32 (offset) + 32 (length) + MAX_CALLS * (32 + 96 + MAX_CALL_DATA) bytes,
# which must fit MAX_CALLBACK_DATA.
MAX_CALLS: constant(uint256) = 4
MAX_CALL_DATA: constant(uint256) = 1888

# The auction allowed to call back during the currently executing take, and
# the payment it quoted in the callback (reported in RouteTaken).
taken_auction: transient(address)
taken_payment: transient(uint256)


@external
def take_with_route(
    _auction: DutchAuction,
    _from: IERC20,
    _max_amount: uint256,
    _min_profit: uint256,
    _profit_receiver: address,
    _calls: DynArray[Call, MAX_CALLS],
) -> uint256:
    """
    @notice Take up to `_max_amount` of an auctioned token, settle it through
            the supplied route, and forward the profit.
    @param _auction Dutch auction to take from.
    @param _from Token offered by the auction.
    @param _max_amount Maximum amount of `_from` to take.
    @param _min_profit Minimum want profit after the payment; reverts below it.
    @param _profit_receiver Receiver of the want profit and unspent `_from`.
    @param _calls Route executed in the take callback; it must leave at least
           the quoted payment in want on this contract.
    @return The want profit forwarded to `_profit_receiver`.
    """
    assert _auction.address != empty(address), ZeroAuction()
    assert _profit_receiver != empty(address), ZeroReceiver()

    self.taken_auction = _auction.address
    amount_taken: uint256 = extcall _auction.take(
        _from.address, _max_amount, self, abi_encode(_calls)
    )
    self.taken_auction = empty(address)

    # Everything left after the auction pulled its payment is profit; unspent
    # lot tokens are swept alongside so nothing stays claimable on the taker.
    want: IERC20 = IERC20(staticcall _auction.want())
    profit: uint256 = staticcall want.balanceOf(self)
    assert profit >= _min_profit, ProfitShortfall()
    if profit != 0:
        assert extcall want.transfer(_profit_receiver, profit, default_return_value=True)
    leftover: uint256 = staticcall _from.balanceOf(self)
    if leftover != 0:
        assert extcall _from.transfer(_profit_receiver, leftover, default_return_value=True)

    log RouteTaken(
        auction=_auction.address,
        token=_from.address,
        caller=msg.sender,
        amount_taken=amount_taken,
        payment=self.taken_payment,
        profit=profit,
    )
    return profit


@external
@reentrant
def auctionTakeCallback(
    _from: address,
    _sender: address,
    _amount_taken: uint256,
    _amount_needed: uint256,
    _data: Bytes[MAX_CALLBACK_DATA],
):
    """
    @notice Auction callback: run the route, then fund the payment pull.
    @dev Reentrant by necessity — the take entrypoint holds the contract-wide
         lock while the auction calls back. Only the transiently recorded
         auction of the active take may enter.
    """
    assert msg.sender == self.taken_auction, OnlyTakenAuction()

    calls: DynArray[Call, MAX_CALLS] = abi_decode(_data, DynArray[Call, MAX_CALLS])
    for call: Call in calls:
        raw_call(call.target, call.data)

    # Exact-amount allowance for the auction's payment pull; the pull returns
    # it to zero in the same transaction. want() is one of the auction's few
    # reentrant views, so it is readable while the auction's lock is held.
    want: IERC20 = IERC20(staticcall DutchAuction(msg.sender).want())
    assert extcall want.approve(msg.sender, _amount_needed, default_return_value=True)
    self.taken_payment = _amount_needed
