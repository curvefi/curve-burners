# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title VeloraDeltaAdapter
@author Curve Finance
@license MIT
@notice Standalone Velora (ex-ParaSwap) Delta settlement adapter for Dutch
        auctions: validates the order digest the Delta settlement asks the
        auction (the order owner) to sign, forwarded by the auction's prefix
        router, against the order it encodes and the auction's shared
        economic order check, and builds publishable orders for agents.
@dev Registry conventions: adapter = this contract, executor = the Delta
     contract itself: the settlement modules run inside it by delegatecall
     and pull the sold tokens with a plain transferFrom from the owner
     (ERC20UtilsLib.transferFrom for an empty permit), so the Delta contract
     is what auctions approve. Signature template: an agent calls
     swapSettle (fill-or-kill) or swapSettleFillable (partial fills) with
     OrderWithSig.signature = this_adapter ++ abi.encode(Order); the
     settlement forwards the bytes verbatim to the owner's (the auction's)
     isValidSignature with the EIP-712 digest of the order under the
     primary type the entry point hashes (Order or FillableOrder). The
     same payload validates under both: the adapter accepts whichever of
     the two digests matches, since the economics are identical (a partial
     fill scales every amount pro rata). The signed destAmount is the floor
     delivered to the beneficiary after the settlement's fees: the
     protocol and a surplus-taking partner are paid only out of the
     surplus above expectedAmount, a partner fee in bps comes off the top
     and is netted out of the floor priced here.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner): the settlement checks the contract
             signature on every fill, so no validated-but-unfilled order
             survives a disable. sync_executor_approvals(settlement, tokens)
             completes the emergency procedure as hygiene.
@custom:security Holds no funds and receives no allowances (the executor —
                 the Delta contract — does, and spends them only under a
                 digest this adapter validated through the auction's
                 router). Auction-agnostic: every economic decision is
                 delegated to msg.sender's check_order, so calling this
                 contract directly proves nothing about any auction.
                 Validity here means the auction accepts the fill;
                 protocol-side liveness (an unused nonce, the registered
                 agent set, the remaining fillable amount) is the
                 publisher's concern and cannot be read from a signature
                 check. The settlement lets a revert of the signature check
                 bubble unchanged (a typed reason reaches the agent) and
                 reports a non-magic answer, such as the router's
                 0xffffffff for a disabled adapter, as its own
                 InvalidSignature.
"""

from contracts.burners.adapters.velora import delta
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadSettlement:
    pass


error BadDomain:
    pass


error NothingToSell:
    pass


interface Settlement:
    def DOMAIN_SEPARATOR() -> bytes32: view


ADAPTER_VERSION: public(constant(String[20])) = "VeloraDeltaAdapter"
# `this_adapter ++ abi_encode(Order)`: the 20-byte adapter address the
# auction's router strips, then the bare order this contract validates.
SIGNATURE_BOUND: constant(uint256) = 20 + delta.ENCODED_ORDER_BOUND

# Protocol wiring pinned at deploy: the EIP-712 domain is fixed by the
# protocol's name and version, this chain and the Delta address, so a
# settlement upgrade means a fresh adapter deployment and a new registry
# entry anyway.
settlement: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_settlement: address):
    """
    @notice Pin the Delta contract and its EIP-712 domain.
    @dev The domain is rebuilt from the fixed name and version, this chain
         and the Delta address and checked against the contract's own
         DOMAIN_SEPARATOR, so a wrong address, a proxy or fork with another
         domain or another protocol version fails the deployment.
    @param _settlement Delta (Portikus V2 adapter) contract of this chain.
    """
    assert _settlement != empty(address), BadSettlement()
    domain: bytes32 = delta._domain_separator(chain.id, _settlement)
    assert domain == staticcall Settlement(_settlement).DOMAIN_SEPARATOR(), BadDomain()

    self.settlement = _settlement
    self.domain_separator = domain


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _nonce: uint256, _sell_amount: uint256 = 0
) -> (delta.Order, Bytes[SIGNATURE_BOUND]):
    """
    @notice Build the order a publisher hands to Delta agents for a live
            lot, together with its signature for the auction.
    @dev Priced at the current block: destAmount and expectedAmount are the
         auction's quote now, so the order stays valid while the curve
         decays below it (a standing ask) and a publisher re-posts, with a
         fresh nonce, as the price falls. Validity ends with the lot window
         (the settlement's deadline is inclusive; check_order rejects at
         lot_end anyway). No partner, no permit (the plain allowance path),
         no metadata, no bridge. Either entry point may fill it: the whole
         amount through swapSettle or pro-rata slices through
         swapSettleFillable. Anyone may publish; only registered agents
         settle.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _nonce Unordered nonce for the auction; the settlement burns it
           once the order is filled in full, so two live orders need
           different nonces.
    @param _sell_amount Amount to sell; 0 sells everything available.
    @return The order and `this_adapter ++ abi_encode(order)`.
    """
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = staticcall _auction.available(_token)
    assert sell_amount > 0, NothingToSell()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)
    buy_amount: uint256 = staticcall _auction.getAmountNeeded(_token, sell_amount)

    order: delta.Order = delta.Order(
        owner=_auction.address,
        beneficiary=staticcall _auction.receiver(),
        srcToken=_token,
        destToken=(staticcall _auction.want()).address,
        srcAmount=sell_amount,
        destAmount=buy_amount,
        expectedAmount=buy_amount,
        deadline=lot_end,
        kind=delta.KIND_SELL,
        nonce=_nonce,
        partnerAndFee=0,
        permit=b"",
        metadata=b"",
        bridge=empty(delta.Bridge),
    )
    return order, concat(convert(self, bytes20), abi_encode(order))


@external
@pure
def describe(_partner_and_fee: uint256) -> (address, uint256, bool, bool):
    """
    @notice Unpack an order's partnerAndFee the way the settlement does.
    @param _partner_and_fee The packed partner address, fee and flags.
    @return Partner address, partner fee (bps), partnerTakesSurplus,
            capSurplus.
    """
    return delta._parse_partner_and_fee(_partner_and_fee)


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[delta.ENCODED_ORDER_BOUND]) -> bytes4:
    """
    @notice Validate a Delta order digest against the calling auction's live
            lot economics.
    @dev The payload is the abi-encoded Order (the router already stripped
         the adapter prefix). Protocol checks revert with
         delta.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The
         calling router lets every revert bubble. A payload that does not
         decode as one order (truncation, dynamic fields above the bounds)
         fails ABI decoding before any check runs and bubbles up the same
         way; one that decodes but is not the order's canonical encoding is
         NonCanonical. The digest must be the order's under either primary
         type (Order or FillableOrder). Shape: the calling auction as
         owner, a sell order, an empty permit (a permit routes the pull
         through Permit2, which the auction never approves, or skips it),
         no bridge (the proceeds must reach the beneficiary on this chain)
         and expectedAmount at or above destAmount (so the surplus split
         never cuts into the floor). Pass-through fields: nonce, metadata,
         the surplus flags and a partner fee, which only lowers the floor
         priced here. The order's amounts are priced as a whole by
         check_order with the beneficiary as receiver (a zero beneficiary
         means the owner, as in the settlement) and the deadline as the
         validity bound.
    @param _hash Order digest the settlement is verifying.
    @param _signature The abi-encoded Order, prefix already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: delta.Order = abi_decode(_signature, delta.Order)
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the order.
    assert abi_encode(order) == _signature, delta.OrderNotValid(reason="NonCanonical")

    assert (
        delta._order_digest(order, self.domain_separator, False) == _hash
        or delta._order_digest(order, self.domain_separator, True) == _hash
    ), delta.OrderNotValid(reason="InvalidHash")
    assert order.owner == msg.sender, delta.OrderNotValid(reason="BadOwner")
    assert order.kind == delta.KIND_SELL, delta.OrderNotValid(reason="BadOrderKind")
    assert len(order.permit) == 0, delta.OrderNotValid(reason="BadPermit")
    assert (
        order.bridge.destinationChainId == 0
        and order.bridge.protocolSelector == empty(bytes4)
        and order.bridge.outputToken == empty(address)
        and order.bridge.scalingFactor == 0
        and len(order.bridge.protocolData) == 0
    ), delta.OrderNotValid(reason="BadBridge")
    assert order.expectedAmount >= order.destAmount, (
        delta.OrderNotValid(reason="BadExpectedAmount")
    )

    receiver: address = order.beneficiary
    if receiver == empty(address):
        receiver = order.owner
    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure. The
    # native-coin placeholder is never a lot (LotInactive) nor the payment
    # token (BadBuyToken).
    assert staticcall IDutchAuction(msg.sender).check_order(
        order.srcToken,
        order.destToken,
        receiver,
        order.srcAmount,
        delta._net_floor(order),
        order.deadline,
    )
    return c.ERC1271_MAGIC_VALUE
