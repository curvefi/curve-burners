# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title WatchtowerHandler
@author Curve Finance
@license MIT
@notice Standalone ComposableCoW IConditionalOrderGenerator for Dutch
        auctions: generates and verifies canonical GPv2 sell orders for the
        watchtower by reading the auction's public views.
@dev Stateless and generic over auction deployments — ComposableCoW passes
     the conditional-order owner, and every quote is read from that auction's
     own quote view at the stable bucket timestamp, so pricing exists in
     exactly one place and this handler holds no math or configuration of its
     own. Discovery only, never authority: the auction validates settlement
     signatures against live lot economics itself, so nothing this handler
     returns can weaken on-chain checks.
@custom:kill Nothing to kill: no owner, no storage, no funds, no approvals.
     Auctions detach by reconfiguring their watchtower wiring to a new
     handler; stale registrations then stop validating generation checks.
"""

from ethereum.ercs import IERC20

from contracts.burners.auction import auction_types
from contracts.burners.cow import gpv2
from contracts.interfaces import IDutchAuctionBurner


# The auction's CoW rail is switched off.
error CowDisabled:
    pass


error ZeroQuote:
    pass


# The CoW protocol constants live with the auction's fallback CowAdapter; the
# auction itself only registers conditional orders.
interface CowAdapter:
    def domain_separator() -> bytes32: view
    def app_data() -> bytes32: view
    def order_validity() -> uint256: view


ERC165_INTERFACE_ID: constant(bytes4) = 0x01ffc9a7

HANDLER_VERSION: public(constant(String[20])) = "WatchtowerHandler"


@internal
@view
def _registered_token(
    _auction: IDutchAuctionBurner,
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
    auction: IDutchAuctionBurner = IDutchAuctionBurner(_owner)
    if not staticcall auction.cow_enabled():
        raise CowDisabled()
    if len(_offchain_input) != 0:
        raise gpv2.OrderNotValid(reason="BadHandlerInput")

    token: address = self._registered_token(auction, _static_input)
    lot: auction_types.Lot = staticcall auction.lots(IERC20(token))
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
    # Quoted by the auction itself at the stable bucket timestamp — signed
    # amounts are bounded by the lot snapshot, not the live remainder.
    buy_amount: uint256 = staticcall auction.quote(token, available, quote_time)
    assert buy_amount > 0, ZeroQuote()

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
    auction: IDutchAuctionBurner = IDutchAuctionBurner(_owner)
    if not staticcall auction.cow_enabled():
        raise CowDisabled()
    if len(_offchain_input) != 0:
        raise gpv2.OrderNotValid(reason="BadHandlerInput")

    token: address = self._registered_token(auction, _static_input)
    lot: auction_types.Lot = staticcall auction.lots(IERC20(token))
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
    # The auction's own quote at the bucket timestamp; a zero quote means the
    # lot went inactive between the liveness gate above and here.
    bucket_quote: uint256 = staticcall auction.quote(token, _order.sellAmount, quote_time)
    if bucket_quote == 0 or _order.buyAmount < bucket_quote:
        raise gpv2.OrderNotValid(reason="BadBuyAmount")


@external
@view
def supportsInterface(_interface_id: bytes4) -> bool:
    """@notice ComposableCoW probes handlers for the generator interface."""
    return _interface_id in [
        ERC165_INTERFACE_ID,
        gpv2.CONDITIONAL_ORDER_GENERATOR_INTERFACE,
    ]
