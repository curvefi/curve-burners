# pragma version 0.5.0b1
# pragma optimize codesize
# The core's unbounded callback type (Bytes[INF]) requires the Venom backend.
# pragma experimental-codegen
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DutchAuctionBurner
@author Curve Finance
@license MIT
@notice Thin FeeCollector wrapper around the Dutch auction core: stages weekly
        fee-token lots and sells them along a geometric Dutch curve through
        native takes and registry adapters (CoW among them) reached by the
        prefix-based ERC-1271 signature router.
@custom:kill Nothing to kill here: FeeCollector kill masks stop all fills,
             and the AdapterRegistry disables adapters (routing stops at once
             for every auction reading it). The owner can recover inventory —
             only back to FeeCollector. Native take and push_target stay
             permissionless while a lot is alive. Disabling an adapter does
             not clear its executor's allowances: batch the registry disable
             with sync_executor_approvals in one transaction.
@custom:migration FeeCollector.set_burner only redirects future staging;
                  nothing detaches here. Calm path: let live lots trade out
                  their window (proceeds still reach FeeCollector), then run
                  the emergency runbook (README) and return leftovers with
                  recover() + push_target(). To stop fills at once, batch
                  recover with FeeCollector.set_killed.
@custom:security The configured start total assumes every staged lot is worth no
                 more than that amount. Inventory accounting is balance-based:
                 available = min(initial_amount, balanceOf), so tokens donated
                 after the weekly snapshot can be resold along the same curve —
                 always at or above the curve price and always in favor of
                 FeeCollector; available never exceeds the snapshot.
                 Executor approvals are infinite but only toward executors of
                 active registry adapters, and only through the permissionless
                 sync. Signature validation is routed to registry adapters;
                 the core's check_order is the economic check offered to
                 them, not enforced by the router, so an adapter that skips
                 it sells by its own rules and listing one is the owner's
                 review. Tokens with transfer fees, rebases, callbacks, or
                 blacklist behavior are best-effort integrations.
"""


from ethereum.ercs import IERC20

from contracts.interfaces import IBurner, IDutchAuction, IFeeCollector, IYearnAuction
from contracts.utils import constants as c, recovery, roles
from contracts.burners.adapters import adapters
from contracts.burners.auction import dutch_auction, yearn_auction

implements: IBurner
implements: IDutchAuction
implements: IYearnAuction
initializes: roles
initializes: dutch_auction
initializes: yearn_auction[dutch_auction := dutch_auction]
initializes: adapters
# Module surfaces are exported method-by-method on purpose: a new external
# function added to a module never enters the burner ABI unreviewed.
# price/getAmountNeeded with a non-current `_ts`: the sellability policy is
# FeeCollector.can_exchange, gated by the live epoch and kill masks, so the
# projection answers 0 outside an open EXCHANGE frame.
exports: (
    dutch_auction.auction_length,
    dutch_auction.want,
    dutch_auction.receiver,
    dutch_auction.start_total,
    dutch_auction.floor_total,
    dutch_auction.step_duration,
    dutch_auction.lots,
    dutch_auction.window,
    dutch_auction.available,
    dutch_auction.price,
    dutch_auction.getAmountNeeded,
    dutch_auction.take,
    dutch_auction.take_with_limits,
    dutch_auction.check_order,
)
# Yearn-only views; to shed them, remove every yearn_auction mention from
# this file.
exports: (
    yearn_auction.isActive,
    yearn_auction.auctionLength,
    yearn_auction.auctions,
)
exports: roles.owner
exports: (
    adapters.registry,
    adapters.sync_executor_approvals,
    adapters.isValidSignature,
)


error BadFeeCollector:
    pass


error BadTarget:
    pass


error BadExchangeFrame:
    pass


error OnlyFeeCollector:
    pass


error TargetChanged:
    pass


error NotSleepEpoch:
    pass


VERSION: public(constant(String[20])) = "DutchAuction"

# FeeCollector integration fixed at deployment. The payment token lives in the
# core as `want`: it mirrors fee_collector.target() at deploy and follows it
# only through the owner's resync_target, so the denomination can never switch
# under a live window.
fee_collector: public(immutable(IFeeCollector))


@deploy
def __init__(
    _fee_collector: IFeeCollector,
    _start_total: uint256,
    _floor_total: uint256,
    _step_duration: uint256,
    _registry: address,
):
    """
    @notice Configure the weekly-auction economics.
    @dev Every lot trades for one EXCHANGE frame: its length becomes the
         core's auction_length and the curve decays exponentially from
         start_total to floor_total over it, so no calibration constant is
         deployed.
    @param _fee_collector FeeCollector: caller of burn, proceeds receiver,
           role source, calendar, and the source of the payment token.
    @param _start_total Want price of a full lot at the window start.
    @param _floor_total Want price of a full lot at the window end.
    @param _step_duration Seconds per price step.
    @param _registry AdapterRegistry read for signature routing and executor
           allowances; an empty one means native settlement only.
    """
    assert _fee_collector.address != empty(address), BadFeeCollector()
    configured_target: address = staticcall _fee_collector.target()
    assert configured_target != empty(address), BadTarget()

    roles.__init__(roles.RoleSource(_fee_collector.address))
    self.fee_collector = _fee_collector
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    assert exchange_end > exchange_start, BadExchangeFrame()
    dutch_auction.__init__(
        IERC20(configured_target),
        _fee_collector.address,
        _start_total,
        _floor_total,
        _step_duration,
        exchange_end - exchange_start,
    )
    adapters.__init__(_registry)


# Shared helpers


@internal
@view
def _target_is_current() -> bool:
    return staticcall self.fee_collector.target() == dutch_auction.want.address


@internal
@view
def _exchange_frame(_timestamp: uint256) -> (uint256, uint256):
    return staticcall self.fee_collector.epoch_time_frame(
        IFeeCollector.Epoch.EXCHANGE, _timestamp
    )


# Weekly staging


@external
def burn(_coins: DynArray[IERC20, c.MAX_COINS], _receiver: address):
    """
    @notice Pay the COLLECT incentive, take custody, and snapshot upcoming lots.
    @dev Staging touches no allowances: the keeper follows up with the
         permissionless sync_executor_approvals so the staged tokens become
         pullable by active adapters. Restaging a leftover lot needs no fresh
         fees: a permissionless FeeCollector.collect during any later COLLECT
         frame re-snapshots the burner's full balance for the upcoming week
         from the top of the curve.
    @param _coins Sorted tokens supplied by FeeCollector.
    @param _receiver Receiver of the FeeCollector COLLECT incentive.
    """
    assert msg.sender == self.fee_collector.address, OnlyFeeCollector()
    assert self._target_is_current(), TargetChanged()

    fee: uint256 = staticcall self.fee_collector.fee(
        IFeeCollector.Epoch.COLLECT, block.timestamp
    )
    fee_payouts: DynArray[IFeeCollector.Transfer, c.MAX_COINS] = []
    custody_transfers: DynArray[IFeeCollector.Transfer, c.MAX_COINS] = []

    for coin: IERC20 in _coins:
        collector_balance: uint256 = staticcall coin.balanceOf(self.fee_collector.address)
        fee_payouts.append(
            IFeeCollector.Transfer(
                coin=coin.address,
                to=_receiver,
                amount=collector_balance * fee // c.WAD,
            )
        )
        custody_transfers.append(
            IFeeCollector.Transfer(coin=coin.address, to=self, amount=max_value(uint256))
        )

    extcall self.fee_collector.transfer(fee_payouts)
    extcall self.fee_collector.transfer(custody_transfers)

    for coin: IERC20 in _coins:
        dutch_auction._stage_lot(coin)


# Auction core integration hooks


@override(dutch_auction)
@view
def _lot_start(_token: IERC20, _staged_at: uint256) -> uint256:
    # The calendar decision lives here, not in the core: a lot staged during
    # a distribution period (its COLLECT frame) trades in that period's
    # EXCHANGE frame, read from the FeeCollector so the calendar is defined
    # in one place. Every token shares the frame; a per-token slot inside it
    # would be this hook's decision alone.
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(_staged_at)
    return exchange_start


@override(dutch_auction)
@view
def _sellable(_token: address) -> bool:
    # The core already excludes the want token; this adds the FeeCollector
    # target-migration and kill-mask checks.
    return self._target_is_current() and staticcall self.fee_collector.can_exchange(
        [_token]
    )



# Economics resync


@external
def resync_target(
    _expected_target: address,
    _start_total: uint256,
    _floor_total: uint256,
    _step_duration: uint256,
):
    """
    @notice Re-pin the payment token to the current fee_collector.target() with
            a curve retuned for it. Also serves as a same-target curve retune.
    @dev The new target is read from the FeeCollector, never passed in, so the
         owner cannot detach the payment denomination from the protocol. The
         curve is solved over auction_length exactly like the constructor.
         Every resync, a same-target retune included, stales
         every lot staged up to its block (the core's resync fence).
         Allowed only during the SLEEP phase — before the week's staging, so
         one configuration governs the entire distribution period: staging,
         the trading window, and forwarding. Consequences: the price within a
         window can never change (staging is confined to COLLECT), so the
         plain Yearn-style take() needs no payment ceiling; the fence only
         ever stales previous weeks' lots and no week is lost; and pairing
         FeeCollector.set_target with the resync inside one SLEEP phase never
         halts collection (burn() rejects a diverged target only in COLLECT).
    @param _expected_target The target the curve parameters were tuned for.
           Governance executes at an uncontrolled time: if the FeeCollector
           target changed again since the vote was drafted, the totals would
           bind to the wrong denomination — execution must revert instead.
    @param _start_total Want price of a full lot at the window start.
    @param _floor_total Want price of a full lot at the window end.
    @param _step_duration Seconds per price step.
    """
    roles._check_owner()
    new_target: address = staticcall self.fee_collector.target()
    assert new_target != empty(address), BadTarget()
    assert new_target == _expected_target, TargetChanged()

    sleep_start: uint256 = 0
    sleep_end: uint256 = 0
    sleep_start, sleep_end = staticcall self.fee_collector.epoch_time_frame(
        IFeeCollector.Epoch.SLEEP, block.timestamp
    )
    assert sleep_start <= block.timestamp and block.timestamp < sleep_end, (
        NotSleepEpoch()
    )

    dutch_auction._set_economics(
        IERC20(new_target),
        _start_total,
        _floor_total,
        _step_duration,
    )


# Recovery and interface discovery


@external
def push_target() -> uint256:
    """
    @notice Permissionlessly return target tokens held by this burner.
    @return Amount of target returned.
    """
    amount: uint256 = staticcall dutch_auction.want.balanceOf(self)
    if amount != 0:
        assert extcall dutch_auction.want.transfer(
            self.fee_collector.address, amount, default_return_value=True
        )
    return amount


@external
def recover(_coins: DynArray[IERC20, c.MAX_COINS]):
    """
    @notice Return ERC-20 or native balances only to FeeCollector.
    @dev Owner-only: stopping fills is done elsewhere (FeeCollector kill
         masks, registry adapter disable); this only moves stuck funds.
         Emptying the balance kills the lot through the balance term of
         available. During the same week's COLLECT frame a permissionless
         collect can pull the token back and restage it, so an evacuation
         batches recover with FeeCollector.set_killed.
    @param _coins Tokens to return in full; ETH_ADDRESS for the native coin.
    """
    roles._check_owner()

    for coin: IERC20 in _coins:
        recovery._recover_coin(coin, self.fee_collector.address)


# Reentrant: answers from constants only.
@external
@view
@reentrant
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @notice Return burner interfaces. ERC-1271 is always claimed: the signature
            router stays live for adapters.
    @param _interface_id ERC-165 interface id.
    @return Whether the interface is supported.
    """
    return _interface_id in [c.ERC165_INTERFACE_ID, c.BURNER_INTERFACE_ID, c.ERC1271_MAGIC_VALUE]
