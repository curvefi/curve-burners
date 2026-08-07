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
    native_remaining: uint256
    start_total: uint256
    floor_total: uint256


interface DutchAuction:
    def cow_enabled() -> bool: view
    def cow_generation() -> uint256: view
    def registered_generation(_token: address) -> uint256: view
    def cow_domain_separator() -> bytes32: view
    def cow_order_validity() -> uint256: view
    def cow_next_poll(_token: address) -> uint256: view
    def app_data() -> bytes32: view
    def want() -> address: view
    def proceeds_receiver() -> address: view
    def decay_factor_ray() -> uint256: view
    def step_duration() -> uint256: view
    def lots(_token: address) -> Lot: view
    def epoch_bounds(_epoch: uint256) -> (uint256, uint256): view
    def available(_token: address) -> uint256: view


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
        gpv2._order_not_valid("BadStaticInput")
    if generation != staticcall _auction.cow_generation():
        gpv2._order_not_valid("StaleGeneration")
    if staticcall _auction.registered_generation(token) != generation:
        gpv2._order_not_valid("OrderNotRegistered")
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
    # Recomputed from the published lot snapshot, epoch window, and curve
    # parameters — must match the auction's own quote at the same timestamp.
    total: uint256 = auction_math.total_price(
        _lot.start_total,
        _lot.floor_total,
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
        gpv2._order_not_valid("BadHandlerInput")

    token: address = self._decode_registered_static_input(auction, _static_input)
    lot: Lot = staticcall auction.lots(token)
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall auction.epoch_bounds(lot.epoch)
    if lot.epoch == 0 or block.timestamp < lot_start or block.timestamp >= lot_end:
        gpv2._poll_try_at(
            staticcall auction.cow_next_poll(token), "NotAllowed"
        )
    available: uint256 = staticcall auction.available(token)
    if available == 0:
        gpv2._poll_try_at(
            staticcall auction.cow_next_poll(token), "ZeroBalance"
        )

    validity: uint256 = staticcall auction.cow_order_validity()
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
        staticcall auction.app_data(),
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
        gpv2._order_not_valid("BadHandlerInput")

    token: address = self._decode_registered_static_input(auction, _static_input)
    lot: Lot = staticcall auction.lots(token)
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall auction.epoch_bounds(lot.epoch)
    if lot.epoch == 0 or block.timestamp < lot_start or block.timestamp >= lot_end:
        gpv2._order_not_valid("NotAllowed")
    # available() folds cancellation, epoch staleness, kill masks, and drained
    # balances into one liveness signal the handler cannot recompute itself.
    if staticcall auction.available(token) == 0:
        gpv2._order_not_valid("NotAllowed")

    if (
        _domain_separator != staticcall auction.cow_domain_separator()
        or gpv2._order_digest(_order, _domain_separator) != _hash
    ):
        gpv2._order_not_valid("InvalidHash")

    validity: uint256 = staticcall auction.cow_order_validity()
    quote_time: uint256 = gpv2._bucket_quote_time(
        block.timestamp, lot_start, validity
    )
    valid_to: uint32 = gpv2._bucket_valid_to(block.timestamp, lot_end, validity)

    if _order.sellToken != token or _order.buyToken != staticcall auction.want():
        gpv2._order_not_valid("BadToken")
    if (
        _order.receiver != staticcall auction.proceeds_receiver()
        or _order.appData != staticcall auction.app_data()
    ):
        gpv2._order_not_valid("BadReceiverOrAppData")
    if not gpv2._check_order_flags(_order):
        gpv2._order_not_valid("BadOrderFlags")
    if not gpv2._check_balance_modes(_order):
        gpv2._order_not_valid("BadBalanceMode")
    if _order.sellAmount == 0 or _order.sellAmount > lot.initial_amount:
        gpv2._order_not_valid("BadSellAmount")
    if _order.validTo != valid_to or convert(_order.validTo, uint256) <= block.timestamp:
        gpv2._order_not_valid("BadValidTo")
    if _order.buyAmount < self._bucket_quote(
        auction, lot, lot_start, _order.sellAmount, quote_time
    ):
        gpv2._order_not_valid("BadBuyAmount")


@external
@view
def supportsInterface(_interface_id: bytes4) -> bool:
    """@notice ComposableCoW probes handlers for the generator interface."""
    return _interface_id in [
        ERC165_INTERFACE_ID,
        gpv2.CONDITIONAL_ORDER_GENERATOR_INTERFACE,
    ]
