# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OneInchFusionAdapter
@author Curve Finance
@license MIT
@notice 1inch Limit Order Protocol v4 settlement adapter for Dutch auctions
        accepting Fusion orders: orders settled through one pinned Fusion
        settlement extension (SimpleSettlement, a FeeTaker with a resolver
        auction rate bump, allowed-time whitelist and surplus fee),
        delivering the taking tokens net of fees to the auction's receiver.
@dev Composes OneInchAdapter as a module: the same router pin, EIP-712
     domain, transport checks and economic delegation; only the extension
     policy differs. Registry conventions and signature template are
     OneInchAdapter's (executor = the protocol, sig = this_adapter ++
     abi.encode(Order, extension)). Economic principle: the signed
     takingAmount is the floor the receiver is paid for makingAmount. The
     fee gross-up and the rate bump only raise what the taker pays and the
     surplus fee only takes from the excess over the (at least linear)
     estimate (fusion.vy), so after the shape checks the order is priced as
     a whole by the auction's check_order exactly like a plain order, with
     the custom receiver of the post-interaction as the receiver; the bump
     curve is not replicated on chain (it depends on block.basefee and only
     adds surplus). Shape: OneInchFeeTakerAdapter's checks against the
     pinned settlement, plus auction details that parse, an auction ending
     no later than the order expiration (afterwards the order stands at its
     linear price, the floor, until it expires), and an estimated taking
     amount at least the order's (the settlement enforces it too). Fills
     are restricted by the settlement to whitelisted resolvers and holders
     of its access token within their allowed times, not by this adapter.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner); native take() stays untouched.
             Because the protocol does not re-check partially filled orders,
             an emergency disable is complete only with
             sync_executor_approvals(protocol, tokens), which zeroes the
             allowance every remaining fill relies on.
@custom:security Holds no funds and receives no allowances. Auction-agnostic:
                 every economic decision is delegated to msg.sender's
                 check_order. Trusts the pinned settlement to implement the
                 verified layout this contract parses (a deploy-time probe
                 checks its rate bump and fee gross-up on a known vector);
                 the taking tokens pass through it within the fill
                 transaction. Fee receivers, whitelists, auction curve and
                 surplus estimate are whatever the publisher encoded: the
                 1inch quote dictates them for relayed orders, and they
                 never reduce what the auction's receiver gets below the
                 linear base.
"""

from contracts.burners.adapters.oneinch import OneInchAdapter as base
from contracts.burners.adapters.oneinch import feetaker
from contracts.burners.adapters.oneinch import fusion
from contracts.burners.adapters.oneinch import oneinch
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c

initializes: base
exports: (
    base.limit_order_protocol,
    base.domain_separator,
)


error BadExtensionContract:
    pass


interface AmountGetter:
    def getTakingAmount(
        _order: oneinch.OrderWords,
        _extension: Bytes[oneinch.MAX_EXTENSION],
        _order_hash: bytes32,
        _taker: address,
        _making_amount: uint256,
        _remaining_making_amount: uint256,
        _extra_data: Bytes[PROBE_DATA_LEN],
    ) -> uint256: view


ADAPTER_VERSION: public(constant(String[32])) = "OneInchFusionAdapter"
PAYLOAD_BOUND: constant(uint256) = base.PAYLOAD_BOUND
SIGNATURE_BOUND: constant(uint256) = 20 + PAYLOAD_BOUND
# The maker traits of a published order: partial and multiple fills, the
# extension flag, the post-interaction call that splits the fees; the
# expiration is added per order.
PUBLISHED_TRAITS: constant(uint256) = (
    oneinch.ALLOW_MULTIPLE_FILLS_FLAG
    | oneinch.HAS_EXTENSION_FLAG
    | oneinch.POST_INTERACTION_CALL_FLAG
)
# Deploy-time probe: an auction that has not started (start time at the
# 32-bit maximum) with a 100% initial rate bump and no gas estimate, then a
# 1% integrator fee with an empty whitelist, on a 1:3 order must price the
# taking amount at exactly 3.03 * 2; a plain FeeTaker reads the same bytes
# as zero fees and an empty whitelist and returns 3.
PROBE_DATA_LEN: constant(uint256) = (
    fusion.AUCTION_HEADER_LEN + feetaker.FEE_DATA_LEN + feetaker.WHITELIST_SIZE_LEN
)
PROBE_DATA: constant(Bytes[PROBE_DATA_LEN]) = (
    x"00000000000000ffffffff0000009896800003e80000000000"
)
PROBE_MAKING: constant(uint256) = 10**18
PROBE_TAKING: constant(uint256) = 3 * 10**18
PROBE_EXPECTED: constant(uint256) = (
    2 * (PROBE_TAKING * (feetaker.BASE_1E5 + 1000) // feetaker.BASE_1E5)
)

# The Fusion settlement every accepted order prices through and pays through.
settlement: public(immutable(address))


@deploy
def __init__(_limit_order_protocol: address, _settlement: address):
    """
    @notice Pin the protocol (its EIP-712 domain cross-checked like
            OneInchAdapter) and the Fusion settlement extension contract.
    @dev The settlement is probed with getTakingAmount on an auction and fee
         vector of the pinned layout, so an address of another kind (a
         plain FeeTaker reads no auction details) fails the deployment.
    @param _limit_order_protocol Limit Order Protocol v4 of this chain.
    @param _settlement The chain's Fusion settlement extension.
    """
    base.__init__(_limit_order_protocol)
    assert _settlement.is_contract, BadExtensionContract()
    probe: oneinch.OrderWords = empty(oneinch.OrderWords)
    probe.makingAmount = PROBE_MAKING
    probe.takingAmount = PROBE_TAKING
    bumped: uint256 = staticcall AmountGetter(_settlement).getTakingAmount(
        probe, b"", empty(bytes32), empty(address), PROBE_MAKING, PROBE_MAKING, PROBE_DATA
    )
    assert bumped == PROBE_EXPECTED, BadExtensionContract()
    self.settlement = _settlement


# Extension policy


@internal
@view
def _check_extension(
    _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256
) -> (feetaker.Fees, feetaker.Receivers, uint256, fusion.AuctionDetails, fusion.Surplus):
    """
    @notice The Fusion shape of an extension: no pre-interaction, both
            amount getters and the post-interaction target the pinned
            settlement, auction details ahead of the getter fee data,
            identical fee data and the same whitelisted takers on both
            sides, the surplus block closing the post-interaction, no
            chained getter or interaction. Returns the fees, the receivers,
            the whitelist size, the auction details and the surplus block.
    """
    assert oneinch._field_is_empty(_offsets, oneinch.PRE_INTERACTION_DATA), (
        oneinch.OrderNotValid(reason="BadPreInteraction")
    )
    getter: Bytes[oneinch.MAX_EXTENSION] = feetaker._amount_getter_data(
        _extension, _offsets, self.settlement
    )
    post: Bytes[oneinch.MAX_EXTENSION] = feetaker._post_interaction_data(
        _extension, _offsets, self.settlement
    )
    details: fusion.AuctionDetails = empty(fusion.AuctionDetails)
    getter_pos: uint256 = 0
    details, getter_pos = fusion._parse_auction_details(getter, 0)
    receivers: feetaker.Receivers = empty(feetaker.Receivers)
    post_pos: uint256 = 0
    receivers, post_pos = feetaker._parse_post_header(post)
    fees: feetaker.Fees = empty(feetaker.Fees)
    size: uint256 = 0
    fees, size, getter_pos, post_pos = feetaker._check_fee_block(
        getter,
        getter_pos,
        post,
        post_pos,
        fusion.POST_WHITELIST_PREFIX_LEN,
        fusion.POST_WHITELIST_ENTRY_LEN,
    )
    assert getter_pos == len(getter), oneinch.OrderNotValid(reason="BadGetterTail")
    surplus: fusion.Surplus = empty(fusion.Surplus)
    surplus, post_pos = fusion._parse_surplus(post, post_pos)
    assert post_pos == len(post), oneinch.OrderNotValid(reason="BadInteractionTail")
    return fees, receivers, size, details, surplus


@internal
@pure
def _check_against_order(
    _details: fusion.AuctionDetails,
    _surplus: fusion.Surplus,
    _taking_amount: uint256,
    _expiration: uint256,
):
    """
    @notice The extension checks that need the order: the auction ends no
            later than the expiration, the estimated taking amount is at
            least the order's (mirrors InvalidEstimatedTakingAmount).
    """
    assert _details.start_time + _details.duration <= _expiration, (
        oneinch.OrderNotValid(reason="AuctionPastExpiration")
    )
    assert _surplus.estimated_taking_amount >= _taking_amount, (
        oneinch.OrderNotValid(reason="InvalidEstimatedTakingAmount")
    )


@internal
@view
def _resolve_receiver(
    _order: oneinch.Order, _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256
) -> address:
    """
    @notice The order-level Fusion checks and the receiver the
            post-interaction pays: the order receiver is the settlement, the
            post-interaction flag is set and the pre-interaction flag is
            not, the extension has the Fusion shape and agrees with the
            order.
    """
    assert (
        oneinch._has_flag(_order.makerTraits, oneinch.POST_INTERACTION_CALL_FLAG)
        and not oneinch._has_flag(_order.makerTraits, oneinch.PRE_INTERACTION_CALL_FLAG)
    ), oneinch.OrderNotValid(reason="BadMakerTraits")
    assert _order.receiver == self.settlement, oneinch.OrderNotValid(reason="BadOrderReceiver")
    fees: feetaker.Fees = empty(feetaker.Fees)
    receivers: feetaker.Receivers = empty(feetaker.Receivers)
    size: uint256 = 0
    details: fusion.AuctionDetails = empty(fusion.AuctionDetails)
    surplus: fusion.Surplus = empty(fusion.Surplus)
    fees, receivers, size, details, surplus = self._check_extension(_extension, _offsets)
    self._check_against_order(
        details, surplus, _order.takingAmount, oneinch._expiration(_order.makerTraits)
    )
    return feetaker._taking_receiver(receivers, _order.maker)


@external
@view
def describe(
    _extension: Bytes[oneinch.MAX_EXTENSION],
) -> (feetaker.Fees, feetaker.Receivers, uint256, fusion.AuctionDetails, fusion.Surplus):
    """
    @notice Parse an extension for publishers: its fees, receivers,
            whitelist size, auction details and surplus block, after the
            same shape checks a fill goes through (reverting with
            oneinch.OrderNotValid(reason) otherwise).
    @param _extension Extension bytes, e.g. built by the 1inch Fusion SDK
           from a quote.
    @return The fee data, the fee and custom receivers, the whitelist size,
            the auction details, the surplus block.
    """
    return self._check_extension(_extension, oneinch._extension_offsets(_extension))


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction,
    _token: address,
    _salt: uint256,
    _sell_amount: uint256,
    _extension: Bytes[oneinch.MAX_EXTENSION],
) -> (oneinch.Order, Bytes[SIGNATURE_BOUND]):
    """
    @notice Build the Fusion order a publisher submits to the 1inch Fusion
            relayer for a live lot, together with its signature for the
            auction.
    @dev The publisher supplies the extension bytes the 1inch Fusion SDK
         builds from a quote (FusionExtension: the quote's or a custom
         preset's auction details, the resolver whitelist, fees and surplus
         estimate, the auction's receiver as custom receiver); this contract
         checks its shape against the lot and fills in the order: priced at
         the current block like OneInchAdapter.order_for (the auction's end
         price should be this quote, every bump above it is surplus),
         receiver = the settlement, partial and multiple fills, the
         post-interaction call, expiring at the lot end (the auction must
         end by then), salt bound to the extension like the SDK binds it
         (the low 96 bits of `_salt` in the high bits). Anyone may publish.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _salt Publisher's salt (96 bits used).
    @param _sell_amount Amount to sell; 0 sells everything available.
    @param _extension The Fusion extension bytes.
    @return The order and `this_adapter ++ abi_encode(order, extension)`.
    """
    sell_amount: uint256 = 0
    taking_amount: uint256 = 0
    lot_end: uint256 = 0
    sell_amount, taking_amount, lot_end = base._quote_lot(_auction, _token, _sell_amount)
    fees: feetaker.Fees = empty(feetaker.Fees)
    receivers: feetaker.Receivers = empty(feetaker.Receivers)
    size: uint256 = 0
    details: fusion.AuctionDetails = empty(fusion.AuctionDetails)
    surplus: fusion.Surplus = empty(fusion.Surplus)
    fees, receivers, size, details, surplus = self._check_extension(
        _extension, oneinch._extension_offsets(_extension)
    )
    self._check_against_order(details, surplus, taking_amount, lot_end)
    assert receivers.custom == staticcall _auction.receiver(), (
        oneinch.OrderNotValid(reason="BadCustomReceiver")
    )

    order: oneinch.Order = oneinch.Order(
        salt=oneinch._bind_salt(_salt, _extension),
        maker=_auction.address,
        receiver=self.settlement,
        makerAsset=_token,
        takerAsset=(staticcall _auction.want()).address,
        makingAmount=sell_amount,
        takingAmount=taking_amount,
        makerTraits=PUBLISHED_TRAITS | (lot_end << oneinch.EXPIRATION_OFFSET),
    )
    return order, concat(convert(self, bytes20), abi_encode(order, _extension))


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[PAYLOAD_BOUND]) -> bytes4:
    """
    @notice Validate a 1inch Fusion order digest against the calling
            auction's live lot economics.
    @dev OneInchAdapter's transport and protocol checks, then the Fusion
         shape (OneInchFeeTakerAdapter's reasons plus BadAuctionDetails,
         BadSurplus, AuctionPastExpiration, InvalidEstimatedTakingAmount),
         then the auction's check_order with the custom receiver (the maker
         when none is flagged, which the auction rejects) as receiver; its
         typed reverts pass through untouched.
    @param _hash Order digest the protocol is verifying.
    @param _signature The abi-encoded (Order, extension) pair, prefix
           already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: oneinch.Order = empty(oneinch.Order)
    extension: Bytes[oneinch.MAX_EXTENSION] = b""
    offsets: uint256 = 0
    order, extension, offsets = base._decode_and_check(_hash, _signature)
    base._check_order(order, self._resolve_receiver(order, extension, offsets))
    return c.ERC1271_MAGIC_VALUE
