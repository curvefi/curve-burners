# pragma version 0.5.0b1
# The unbounded route type (Bytes[INF]) requires the Venom backend.
# pragma experimental-codegen
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
@dev The route is a list of raw calls executed inside auctionTakeCallback,
     after the lot has been delivered and before the auction pulls the
     payment from this contract's want allowance. A typical plan is
     [approve(router, lot), router.swap(lot -> want)]. It is passed as packed
     bytes, one step after another with no padding:
         target (20 bytes) ++ calldata length (2 bytes, big endian) ++ calldata
     and handed to the auction as the callback data unchanged, so a route
     costs what its own bytes cost and has no size the contract reserves
     memory for; only the number of steps is bounded. The route must
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


error EmptyRoute:
    pass


error BadRoute:
    pass


event RouteTaken:
    auction: indexed(address)
    token: indexed(address)
    caller: indexed(address)
    amount_taken: uint256
    payment: uint256
    profit: uint256


# A packed route step: target ++ calldata length ++ calldata.
TARGET_SIZE: constant(uint256) = 20
LENGTH_SIZE: constant(uint256) = 2
STEP_HEADER: constant(uint256) = TARGET_SIZE + LENGTH_SIZE
# Bound of the step loop; a route's length in bytes is not bounded.
MAX_STEPS: constant(uint256) = 256

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
    _route: Bytes[INF],
) -> uint256:
    """
    @notice Take up to `_max_amount` of an auctioned token, settle it through
            the supplied route, and forward the profit.
    @param _auction Dutch auction to take from.
    @param _from Token offered by the auction.
    @param _max_amount Maximum amount of `_from` to take.
    @param _min_profit Minimum want profit after the payment; reverts below it.
    @param _profit_receiver Receiver of the want profit and unspent `_from`.
    @param _route Packed route steps executed in the take callback; they must
           leave at least the quoted payment in want on this contract.
    @return The want profit forwarded to `_profit_receiver`.
    """
    assert _auction.address != empty(address), ZeroAuction()
    assert _profit_receiver != empty(address), ZeroReceiver()
    # The auction calls back only with data, and without the callback there
    # is no allowance for its payment pull.
    assert len(_route) != 0, EmptyRoute()

    self.active_auction = _auction.address
    amount_taken: uint256 = extcall _auction.take(_from.address, _max_amount, self, _route)
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
    _data: Bytes[INF],
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
    @param _data Packed route steps, as passed to take_with_route.
    """
    assert msg.sender == self.active_auction, OnlyActiveAuction()

    offset: uint256 = 0
    for i: uint256 in range(MAX_STEPS):
        if offset == len(_data):
            break
        target: address = convert(
            convert(slice(_data, offset, TARGET_SIZE), bytes20), address
        )  # dev: truncated step
        size: uint256 = convert(
            convert(slice(_data, offset + TARGET_SIZE, LENGTH_SIZE), bytes2), uint256
        )  # dev: truncated step
        # A failed route step reverts the whole take; no return data is read.
        raw_call(
            target, slice(_data, offset + STEP_HEADER, size), revert_on_failure=True
        )  # dev: truncated step
        offset += STEP_HEADER + size
    # Steps past MAX_STEPS are never silently dropped.
    assert offset == len(_data), BadRoute()
    # Exact-amount allowance for the auction's payment pull; the pull returns
    # it to zero in the same transaction. want() is reentrant, so it is
    # readable while the auction's lock is held.
    want: IERC20 = staticcall IDutchAuction(msg.sender).want()
    assert extcall want.approve(msg.sender, _amount_needed, default_return_value=True)
    self.quoted_payment = _amount_needed
