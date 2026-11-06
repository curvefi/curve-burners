# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OneInchFeeTakerAdapter
@author Curve Finance
@license MIT
@notice 1inch Limit Order Protocol v4 settlement adapter for Dutch auctions
        accepting orders with the standard FeeTaker extension, the only
        format the public 1inch orderbook API relays: the fee-grossed amount
        getters and the fee-splitting post-interaction of one pinned
        FeeTaker contract, delivering the taking tokens net of fees to the
        auction's receiver.
@dev Composes OneInchAdapter as a module: the same router pin, EIP-712
     domain, transport checks and economic delegation; only the extension
     policy differs. Registry conventions and signature template are
     OneInchAdapter's (executor = the protocol, sig = this_adapter ++
     abi.encode(Order, extension)). Economic principle: the signed
     takingAmount is the floor the receiver is paid for makingAmount. The
     FeeTaker only grosses what the taker pays up (feetaker.vy), so after
     the shape checks the order is priced as a whole by the auction's
     check_order exactly like a plain order, with the custom receiver of the
     post-interaction as the receiver. Shape: the order receiver is the
     FeeTaker (the post-interaction splits only then), the post-interaction
     flag is set (the protocol calls the listener only then; without it the
     taking tokens would stay with the FeeTaker), no pre-interaction, both
     amount getters and the post-interaction target the pinned FeeTaker with
     the same fee data and whitelist, no chained getter or interaction. Fee
     values are not bounded: they are the taker's cost. Fills are
     restricted by the FeeTaker to whitelisted takers and holders of its
     access token (1inch resolvers), not by this adapter.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner); native take() stays untouched.
             Because the protocol does not re-check partially filled orders,
             an emergency disable is complete only with
             sync_executor_approvals(protocol, tokens), which zeroes the
             allowance every remaining fill relies on.
@custom:security Holds no funds and receives no allowances. Auction-agnostic:
                 every economic decision is delegated to msg.sender's
                 check_order. Trusts the pinned FeeTaker to implement the
                 verified layout this contract parses (a deploy-time probe
                 checks its fee gross-up on a known vector); the taking
                 tokens pass through it within the fill transaction. Fee
                 receivers and whitelists are whatever the publisher
                 encoded: the 1inch API dictates them for relayed orders,
                 and they never reduce what the auction's receiver gets
                 below the linear base.
"""

from contracts.burners.adapters.oneinch import OneInchAdapter as base
from contracts.burners.adapters.oneinch import feetaker
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


ADAPTER_VERSION: public(constant(String[32])) = "OneInchFeeTakerAdapter"
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
# Deploy-time probe: a 1% integrator fee with an empty whitelist on a 1:3
# order must gross the taking amount up to exactly 3.03.
PROBE_DATA_LEN: constant(uint256) = feetaker.FEE_DATA_LEN + feetaker.WHITELIST_SIZE_LEN
PROBE_DATA: constant(Bytes[PROBE_DATA_LEN]) = x"03e80000000000"
PROBE_MAKING: constant(uint256) = 10**18
PROBE_TAKING: constant(uint256) = 3 * 10**18
PROBE_EXPECTED: constant(uint256) = PROBE_TAKING * (feetaker.BASE_1E5 + 1000) // feetaker.BASE_1E5

# The FeeTaker every accepted order prices through and pays through.
fee_taker: public(immutable(address))


@deploy
def __init__(_limit_order_protocol: address, _fee_taker: address):
    """
    @notice Pin the protocol (its EIP-712 domain cross-checked like
            OneInchAdapter) and the FeeTaker extension contract.
    @dev The FeeTaker is probed with getTakingAmount on a fee vector of the
         pinned layout, so an address of another kind (a Fusion settlement
         reads auction details first) fails the deployment.
    @param _limit_order_protocol Limit Order Protocol v4 of this chain.
    @param _fee_taker The chain's FeeTaker extension.
    """
    base.__init__(_limit_order_protocol)
    assert _fee_taker.is_contract, BadExtensionContract()
    probe: oneinch.OrderWords = empty(oneinch.OrderWords)
    probe.makingAmount = PROBE_MAKING
    probe.takingAmount = PROBE_TAKING
    grossed: uint256 = staticcall AmountGetter(_fee_taker).getTakingAmount(
        probe, b"", empty(bytes32), empty(address), PROBE_MAKING, PROBE_MAKING, PROBE_DATA
    )
    assert grossed == PROBE_EXPECTED, BadExtensionContract()
    self.fee_taker = _fee_taker


# Extension policy


@internal
@view
def _check_extension(
    _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256
) -> (feetaker.Fees, feetaker.Receivers, uint256):
    """
    @notice The FeeTaker shape of an extension: no pre-interaction, both
            amount getters and the post-interaction target the pinned
            FeeTaker, identical fee data and whitelist on both sides, no
            chained getter or interaction. Returns the fees, the receivers
            and the whitelist size.
    """
    assert oneinch._field_is_empty(_offsets, oneinch.PRE_INTERACTION_DATA), (
        oneinch.OrderNotValid(reason="BadPreInteraction")
    )
    getter: Bytes[oneinch.MAX_EXTENSION] = feetaker._amount_getter_data(
        _extension, _offsets, self.fee_taker
    )
    post: Bytes[oneinch.MAX_EXTENSION] = feetaker._post_interaction_data(
        _extension, _offsets, self.fee_taker
    )
    receivers: feetaker.Receivers = empty(feetaker.Receivers)
    post_pos: uint256 = 0
    receivers, post_pos = feetaker._parse_post_header(post)
    fees: feetaker.Fees = empty(feetaker.Fees)
    size: uint256 = 0
    getter_pos: uint256 = 0
    fees, size, getter_pos, post_pos = feetaker._check_fee_block(
        getter, 0, post, post_pos, 0, feetaker.WHITELIST_ENTRY_LEN
    )
    assert getter_pos == len(getter), oneinch.OrderNotValid(reason="BadGetterTail")
    assert post_pos == len(post), oneinch.OrderNotValid(reason="BadInteractionTail")
    return fees, receivers, size


@internal
@view
def _resolve_receiver(
    _order: oneinch.Order, _extension: Bytes[oneinch.MAX_EXTENSION], _offsets: uint256
) -> address:
    """
    @notice The order-level FeeTaker checks and the receiver the
            post-interaction pays: the order receiver is the FeeTaker, the
            post-interaction flag is set and the pre-interaction flag is
            not, the extension has the FeeTaker shape.
    """
    assert (
        oneinch._has_flag(_order.makerTraits, oneinch.POST_INTERACTION_CALL_FLAG)
        and not oneinch._has_flag(_order.makerTraits, oneinch.PRE_INTERACTION_CALL_FLAG)
    ), oneinch.OrderNotValid(reason="BadMakerTraits")
    assert _order.receiver == self.fee_taker, oneinch.OrderNotValid(reason="BadOrderReceiver")
    fees: feetaker.Fees = empty(feetaker.Fees)
    receivers: feetaker.Receivers = empty(feetaker.Receivers)
    size: uint256 = 0
    fees, receivers, size = self._check_extension(_extension, _offsets)
    return feetaker._taking_receiver(receivers, _order.maker)


@external
@view
def describe(
    _extension: Bytes[oneinch.MAX_EXTENSION],
) -> (feetaker.Fees, feetaker.Receivers, uint256):
    """
    @notice Parse an extension for publishers: its fees, receivers and
            whitelist size, after the same shape checks a fill goes through
            (reverting with oneinch.OrderNotValid(reason) otherwise).
    @param _extension Extension bytes, e.g. built by the 1inch SDK from the
           orderbook fee-info.
    @return The fee data, the fee and custom receivers, the whitelist size.
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
    @notice Build the FeeTaker order a publisher relays through the 1inch
            orderbook for a live lot, together with its signature for the
            auction.
    @dev The publisher supplies the extension bytes the 1inch SDK builds
         from the orderbook fee-info (FeeTakerExtension with the auction's
         receiver as custom receiver); this contract checks its shape and
         custom receiver and fills in the order: priced at the current block
         like OneInchAdapter.order_for (a standing ask re-posted lower with
         a fresh salt as the curve decays), receiver = the FeeTaker, partial
         and multiple fills, the post-interaction call, expiring at the lot
         end, salt bound to the extension like the SDK binds it (the low 96
         bits of `_salt` in the high bits). Anyone may publish.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _salt Publisher's salt (96 bits used).
    @param _sell_amount Amount to sell; 0 sells everything available.
    @param _extension The FeeTaker extension bytes.
    @return The order and `this_adapter ++ abi_encode(order, extension)`.
    """
    sell_amount: uint256 = 0
    taking_amount: uint256 = 0
    lot_end: uint256 = 0
    sell_amount, taking_amount, lot_end = base._quote_lot(_auction, _token, _sell_amount)
    fees: feetaker.Fees = empty(feetaker.Fees)
    receivers: feetaker.Receivers = empty(feetaker.Receivers)
    size: uint256 = 0
    fees, receivers, size = self._check_extension(
        _extension, oneinch._extension_offsets(_extension)
    )
    assert receivers.custom == staticcall _auction.receiver(), (
        oneinch.OrderNotValid(reason="BadCustomReceiver")
    )

    order: oneinch.Order = oneinch.Order(
        salt=oneinch._bind_salt(_salt, _extension),
        maker=_auction.address,
        receiver=self.fee_taker,
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
    @notice Validate a 1inch FeeTaker order digest against the calling
            auction's live lot economics.
    @dev OneInchAdapter's transport and protocol checks (NonCanonical,
         InvalidHash, BadMaker, BadMakerTraits, extension binding,
         BadAssetSuffix), then the FeeTaker shape (BadMakerTraits for the
         interaction flags, BadOrderReceiver, BadPreInteraction,
         BadAmountGetter, BadPostInteraction, BadFeeData, BadWhitelist,
         BadGetterTail, BadInteractionTail, BadExtensionOffsets), then the
         auction's check_order with the custom receiver (the maker when none
         is flagged, which the auction rejects) as receiver; its typed
         reverts pass through untouched.
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
