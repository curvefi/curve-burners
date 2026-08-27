# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CowWatchtowerHandler
@author Curve Finance
@license MIT
@notice Standalone ComposableCoW IConditionalOrderGenerator for Dutch
        auctions: generates and verifies canonical GPv2 sell orders for the
        watchtower by reading the auction's public views.
@dev Stateless and generic over auction deployments — ComposableCoW passes
     the conditional-order owner, and every quote is recomputed from that
     auction's published lot record and curve parameters via the shared
     auction math, so this handler holds no configuration of its own.
     Discovery only, never authority: the auction validates settlement
     signatures against live lot economics itself, so nothing this handler
     returns can weaken on-chain checks.
@custom:kill Nothing to kill: no owner, no storage, no funds, no approvals.
     Auctions detach by reconfiguring their watchtower wiring to a new
     handler; stale registrations then stop validating generation checks.
"""

from ..auction import dutch_auction_math as auction_math
from . import gpv2


error ZeroCowQuote:
    pass


# Mirror of the auction core's lot record. Time bounds are not part of the
# record: the auction's calendar publishes them via epoch_bounds.
struct Lot:
    epoch: uint256
    initial_amount: uint256


interface DutchAuction:
    def cow_enabled() -> bool: view
    def fallback_adapter() -> address: view
    def cow_generation() -> uint256: view
    def registered_generation(_token: address) -> uint256: view
    def cow_next_poll(_token: address) -> uint256: view
    def want() -> address: view
    def proceeds_receiver() -> address: view
    def start_total() -> uint256: view
    def floor_total() -> uint256: view
    def decay_factor_ray() -> uint256: view
    def step_duration() -> uint256: view
    def lots(_token: address) -> Lot: view
    def epoch_bounds(_epoch: uint256) -> (uint256, uint256): view
    def available(_token: address) -> uint256: view


# The CoW protocol constants live with the auction's fallback CowAdapter; the
# auction itself only registers conditional orders.
interface CowAdapter:
    def domain_separator() -> bytes32: view
    def app_data() -> bytes32: view
    def order_validity() -> uint256: view


ERC165_INTERFACE_ID: constant(bytes4) = 0x01ffc9a7

HANDLER_VERSION: public(constant(String[20])) = "DutchAuctionHandler"


@internal
@view
def _decode_registered_static_input(
    _auction: DutchAuction,
    _static_input: Bytes[gpv2.MAX_HANDLER_INPUT_LEN],
) -> address:
    ok: bool = False
    token: address = empty(address)
    generation: uint256 = 0
    ok, token, generation = gpv2._decode_static_input(_static_input)
    if not ok:
        raise gpv2.OrderNotValid(reason="BadStaticInput")
    if generation != staticcall _auction.cow_generation():
        raise gpv2.OrderNotValid(reason="StaleGeneration")
    if staticcall _auction.registered_generation(token) != generation:
        raise gpv2.OrderNotValid(reason="OrderNotRegistered")
    return token


@internal
@view
def _bucket_quote(
    _auction: DutchAuction,
    _lot: Lot,
    _lot_start: uint256,
    _amount: uint256,
    _quote_time: uint256,
) -> uint256:
    # Recomputed from the published lot snapshot, epoch window, and live curve
    # parameters — must match the auction's own quote at the same timestamp.
    total: uint256 = auction_math.total_price(
        staticcall _auction.start_total(),
        staticcall _auction.floor_total(),
        staticcall _auction.decay_factor_ray(),
        _quote_time - _lot_start,
        staticcall _auction.step_duration(),
    )
    return auction_math.proportional_payment(total, _amount, _lot.initial_amount)


@external
@view
def getTradeableOrder(
    _owner: address,
    _sender: address,
    _ctx: bytes32,
    _static_input: Bytes[gpv2.MAX_HANDLER_INPUT_LEN],
    _offchain_input: Bytes[gpv2.MAX_OFFCHAIN_INPUT_LEN],
) -> gpv2.GPv2Order:
    """Generate a canonical, generation-aware GPv2 sell order for a watchtower."""
    auction: DutchAuction = DutchAuction(_owner)
    if not staticcall auction.cow_enabled():
        raise gpv2.CowDisabled()
    if len(_offchain_input) != 0:
        raise gpv2.OrderNotValid(reason="BadHandlerInput")

    token: address = self._decode_registered_static_input(auction, _static_input)
    lot: Lot = staticcall auction.lots(token)
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall auction.epoch_bounds(lot.epoch)
    if lot.epoch == 0 or block.timestamp < lot_start or block.timestamp >= lot_end:
        raise gpv2.PollTryAtEpoch(
            timestamp=staticcall auction.cow_next_poll(token), reason="NotAllowed"
        )
    available: uint256 = staticcall auction.available(token)
    if available == 0:
        raise gpv2.PollTryAtEpoch(
            timestamp=staticcall auction.cow_next_poll(token), reason="ZeroBalance"
        )

    adapter: CowAdapter = CowAdapter(staticcall auction.fallback_adapter())
    validity: uint256 = staticcall adapter.order_validity()
    quote_time: uint256 = gpv2._bucket_quote_time(
        block.timestamp, lot_start, validity
    )
    valid_to: uint32 = gpv2._bucket_valid_to(block.timestamp, lot_end, validity)
    buy_amount: uint256 = self._bucket_quote(auction, lot, lot_start, available, quote_time)
    assert buy_amount > 0, ZeroCowQuote()

    return gpv2._build_sell_order(
        token,
        staticcall auction.want(),
        staticcall auction.proceeds_receiver(),
        available,
        buy_amount,
        valid_to,
        staticcall adapter.app_data(),
    )


@external
@view
def verify(
    _owner: address,
    _sender: address,
    _hash: bytes32,
    _domain_separator: bytes32,
    _ctx: bytes32,
    _static_input: Bytes[gpv2.MAX_HANDLER_INPUT_LEN],
    _offchain_input: Bytes[gpv2.MAX_OFFCHAIN_INPUT_LEN],
    _order: gpv2.GPv2Order,
):
    """Validate every economic GPv2 field and reject disabled or stale conditional orders."""
    auction: DutchAuction = DutchAuction(_owner)
    if not staticcall auction.cow_enabled():
        raise gpv2.CowDisabled()
    if len(_offchain_input) != 0:
        raise gpv2.OrderNotValid(reason="BadHandlerInput")

    token: address = self._decode_registered_static_input(auction, _static_input)
    lot: Lot = staticcall auction.lots(token)
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall auction.epoch_bounds(lot.epoch)
    if lot.epoch == 0 or block.timestamp < lot_start or block.timestamp >= lot_end:
        raise gpv2.OrderNotValid(reason="NotAllowed")
    # available() folds epoch staleness, kill masks, and drained balances
    # into one liveness signal the handler cannot recompute itself.
    if staticcall auction.available(token) == 0:
        raise gpv2.OrderNotValid(reason="NotAllowed")

    adapter: CowAdapter = CowAdapter(staticcall auction.fallback_adapter())
    if (
        _domain_separator != staticcall adapter.domain_separator()
        or gpv2._order_digest(_order, _domain_separator) != _hash
    ):
        raise gpv2.OrderNotValid(reason="InvalidHash")

    validity: uint256 = staticcall adapter.order_validity()
    quote_time: uint256 = gpv2._bucket_quote_time(
        block.timestamp, lot_start, validity
    )
    valid_to: uint32 = gpv2._bucket_valid_to(block.timestamp, lot_end, validity)

    if _order.sellToken != token or _order.buyToken != staticcall auction.want():
        raise gpv2.OrderNotValid(reason="BadToken")
    if (
        _order.receiver != staticcall auction.proceeds_receiver()
        or _order.appData != staticcall adapter.app_data()
    ):
        raise gpv2.OrderNotValid(reason="BadReceiverOrAppData")
    if not gpv2._check_order_flags(_order):
        raise gpv2.OrderNotValid(reason="BadOrderFlags")
    if not gpv2._check_balance_modes(_order):
        raise gpv2.OrderNotValid(reason="BadBalanceMode")
    if _order.sellAmount == 0 or _order.sellAmount > lot.initial_amount:
        raise gpv2.OrderNotValid(reason="BadSellAmount")
    if _order.validTo != valid_to or convert(_order.validTo, uint256) <= block.timestamp:
        raise gpv2.OrderNotValid(reason="BadValidTo")
    if _order.buyAmount < self._bucket_quote(
        auction, lot, lot_start, _order.sellAmount, quote_time
    ):
        raise gpv2.OrderNotValid(reason="BadBuyAmount")


@external
@view
def supportsInterface(_interface_id: bytes4) -> bool:
    """@notice ComposableCoW probes handlers for the generator interface."""
    return _interface_id in [
        ERC165_INTERFACE_ID,
        gpv2.CONDITIONAL_ORDER_GENERATOR_INTERFACE,
    ]
