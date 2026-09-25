# pragma version 0.5.0b1
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
     leave at least the quoted payment in want on this contract; the want
     above it is the profit, checked against the caller's minimum and sent
     to the caller-chosen profit receiver together with any unspent lot
     tokens.
@custom:kill Nothing to kill: no owner, no configuration, and no funds or
             allowances at rest — every take resets the active auction and
             sweeps both tokens in the same transaction (the quoted payment
             only feeds the event). Retire it by no longer routing fills
             through it.
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

from contracts.interfaces import IDutchAuction


error ZeroAuction:
    pass


error ZeroReceiver:
    pass


error OnlyActiveAuction:
    pass


error ProfitShortfall:
    pass


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


# The auction forwards callback data unbounded, so the bound is set on this
# side: longer data simply fails to decode. abi_encode(DynArray[Call, MAX_CALLS])
# worst case is 32 (offset) + 32 (length) + MAX_CALLS * (32 + 96 + MAX_CALL_DATA)
# bytes, which must fit MAX_CALLBACK_DATA.
MAX_CALLBACK_DATA: constant(uint256) = 8192
MAX_CALLS: constant(uint256) = 4
MAX_CALL_DATA: constant(uint256) = 1888

# The auction allowed to call back during the currently executing take, and
# the payment it quoted in the callback (reported in RouteTaken).
active_auction: transient(address)
quoted_payment: transient(uint256)


@external
def take_with_route(
    _auction: IDutchAuction,
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

    self.active_auction = _auction.address
    amount_taken: uint256 = extcall _auction.take(
        _from.address, _max_amount, self, abi_encode(_calls)
    )
    self.active_auction = empty(address)

    # Everything left after the auction pulled its payment is profit; unspent
    # lot tokens are swept alongside so nothing stays claimable on the taker.
    want: IERC20 = staticcall _auction.want()
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
        payment=self.quoted_payment,
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
    @param _from Token taken from the auction.
    @param _sender Caller of the auction's take (this contract).
    @param _amount_taken Amount of `_from` delivered to this contract.
    @param _amount_needed Want payment the auction pulls after this callback.
    @param _data abi-encoded route (DynArray[Call, MAX_CALLS]).
    """
    assert msg.sender == self.active_auction, OnlyActiveAuction()

    calls: DynArray[Call, MAX_CALLS] = abi_decode(_data, DynArray[Call, MAX_CALLS])
    for call: Call in calls:
        # A failed route step reverts the whole take; no return data is read.
        raw_call(call.target, call.data, revert_on_failure=True)

    # Exact-amount allowance for the auction's payment pull; the pull returns
    # it to zero in the same transaction. want() is reentrant, so it is
    # readable while the auction's lock is held.
    want: IERC20 = staticcall IDutchAuction(msg.sender).want()
    assert extcall want.approve(msg.sender, _amount_needed, default_return_value=True)
    self.quoted_payment = _amount_needed
