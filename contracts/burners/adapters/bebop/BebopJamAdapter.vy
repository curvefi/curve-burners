# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title BebopJamAdapter
@author Curve Finance
@license MIT
@notice Standalone Bebop JAM v2 settlement adapter for Dutch auctions:
        validates the order digest JamSettlement asks the auction (the JAM
        taker) to sign, forwarded by the auction's prefix router, against
        the order it encodes and the auction's shared economic order check,
        and builds publishable orders for solvers.
@dev Registry conventions: adapter = this contract, executor = the
     settlement's JamBalanceManager: a fill pulls the sell tokens with a
     plain transferFrom from the taker through the balance manager, so the
     balance manager is what auctions approve (orders pulled through Permit2
     are not signed by this adapter, see isValidSignature). Signature
     template: a solver calls settle/settleInternal/settleBatch with
     signature = this_adapter ++ abi.encode(JamOrder) where JamOrder is the
     twelve signed fields (the API's `toSign` object, hooksHash included);
     the settlement forwards the bytes verbatim to the taker's (the
     auction's) isValidSignature with the EIP-712 digest. Every JAM order
     is fill-or-kill: the signed sellAmount is pulled in full and the signed
     buyAmount is the floor paid to the receiver (partner and protocol fees
     come out of the solver's surplus above it), and the nonce is burnt on
     the fill, so an order never settles twice.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner): the settlement checks the contract
             signature on every fill, so no validated-but-unfilled order
             survives a disable. sync_executor_approvals(balance_manager,
             tokens) completes the emergency procedure as hygiene.
@custom:security Holds no funds and receives no allowances (the executor —
                 the balance manager — does, and spends them only under a
                 digest this adapter validated through the auction's
                 router). Auction-agnostic: every economic decision is
                 delegated to msg.sender's check_order, so calling this
                 contract directly proves nothing about any auction.
                 Validity here means the auction accepts the fill;
                 protocol-side liveness (nonce, executor exclusivity,
                 strict expiry) is the publisher's concern and cannot be
                 read from a signature check. The settlement lets a revert
                 of the signature check bubble unchanged (a typed reason
                 reaches the solver) and reports a non-magic answer, such
                 as the router's 0xffffffff for a disabled adapter, as its
                 own InvalidContractSignature.
"""

from contracts.burners.adapters.bebop import jam
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadSettlement:
    pass


error BadDomain:
    pass


error BadBalanceManager:
    pass


error NothingToSell:
    pass


interface Settlement:
    def DOMAIN_SEPARATOR() -> bytes32: view
    def balanceManager() -> address: view
    def hashJamOrder(_order: jam.JamOrderCalldata, _hooks_hash: bytes32) -> bytes32: view


ADAPTER_VERSION: public(constant(String[20])) = "BebopJamAdapter"
# `this_adapter ++ abi_encode(JamOrder)`: the 20-byte adapter address the
# auction's router strips, then the bare order this contract validates. A
# published order has one token per side (jam.ENCODED_SINGLE_ORDER_LEN
# bytes); the bound is the encoder's.
SIGNATURE_BOUND: constant(uint256) = 20 + jam.ENCODED_ORDER_BOUND

# Protocol wiring pinned at deploy: the EIP-712 domain is fixed by the
# settlement's name, version and address and the balance manager is created
# by the settlement's constructor, so a settlement upgrade means a fresh
# adapter deployment and a new registry entry anyway.
settlement: public(immutable(address))
domain_separator: public(immutable(bytes32))
balance_manager: public(immutable(address))


@deploy
def __init__(_settlement: address):
    """
    @notice Pin the settlement, its EIP-712 domain and its balance manager.
    @dev The domain is rebuilt from the fixed name and version, this chain
         and the settlement address and checked against the settlement's
         DOMAIN_SEPARATOR and its own hashJamOrder on a probe order, so a
         wrong address, a proxy or fork with another domain, another
         protocol version or a different order type fails the deployment.
    @param _settlement JamSettlement of this chain.
    """
    assert _settlement != empty(address), BadSettlement()
    protocol: Settlement = Settlement(_settlement)
    domain: bytes32 = jam._domain_separator(chain.id, _settlement)
    assert domain == staticcall protocol.DOMAIN_SEPARATOR(), BadDomain()
    probe: jam.JamOrder = empty(jam.JamOrder)
    probe_hash: bytes32 = staticcall protocol.hashJamOrder(
        jam._to_calldata(probe), jam.EMPTY_HOOKS_HASH
    )
    assert probe_hash == jam._order_digest(probe, domain), BadDomain()
    manager: address = staticcall protocol.balanceManager()
    assert manager != empty(address), BadBalanceManager()

    self.settlement = _settlement
    self.domain_separator = domain
    self.balance_manager = manager


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _nonce: uint256, _sell_amount: uint256 = 0
) -> (jam.JamOrder, Bytes[SIGNATURE_BOUND]):
    """
    @notice Build the order a publisher hands to JAM solvers for a live lot,
            together with its signature for the auction.
    @dev Priced at the current block: the buy amount is the auction's quote
         now, so the order stays valid while the curve decays below it (a
         standing ask) and a publisher re-posts, with a fresh nonce, as the
         price falls. Fill-or-kill by protocol design: the whole sell
         amount against at least the buy amount. Validity ends with the
         lot window (the settlement's expiry is exclusive, so the last
         second of the window is already out). Any executor, no
         exclusivity, no fees, no hooks. Anyone may publish; anyone may
         fill through settle or settleInternal.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _nonce Order nonce (nonzero); the settlement burns it on the
           fill, so two live orders need different nonces.
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

    order: jam.JamOrder = jam.JamOrder(
        taker=_auction.address,
        receiver=staticcall _auction.receiver(),
        expiry=lot_end,
        exclusivityDeadline=0,
        nonce=_nonce,
        executor=empty(address),
        partnerInfo=0,
        sellTokens=[_token],
        buyTokens=[(staticcall _auction.want()).address],
        sellAmounts=[sell_amount],
        buyAmounts=[staticcall _auction.getAmountNeeded(_token, sell_amount)],
        hooksHash=jam.EMPTY_HOOKS_HASH,
    )
    return order, concat(convert(self, bytes20), abi_encode(order))


@external
@pure
def describe(_partner_info: uint256) -> (address, uint256, uint256):
    """
    @notice Unpack an order's partnerInfo the way the settlement does.
    @param _partner_info The packed partner address and fees.
    @return Partner address, partner fee (bps), protocol fee (bps).
    """
    return jam._unpack_partner_info(_partner_info)


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[jam.ENCODED_ORDER_BOUND]) -> bytes4:
    """
    @notice Validate a JAM order digest against the calling auction's live
            lot economics.
    @dev The payload is the abi-encoded signed JamOrder (the router already
         stripped the adapter prefix). Protocol checks revert with
         jam.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The
         calling router lets every revert bubble. A payload that does not
         decode as one order (truncation, arrays above the bound) fails ABI
         decoding before any check runs and bubbles up the same way; one
         that decodes but is not the order's canonical encoding is
         NonCanonical. Shape: the calling auction as taker, no hooks (hooks
         are calls the settlement makes on the taker's behalf), exactly one
         sell and one buy token, a nonzero nonce, and a partnerInfo the
         settlement accepts (fees only ever reduce the solver's surplus
         above the signed buy amount). Pass-through fields: executor and
         exclusivityDeadline only restrict who fills, partner and protocol
         fees are the solver's cost. The order's amounts are priced as a
         whole by check_order with the receiver as signed and the expiry as
         the validity bound. Orders pulled through Permit2 never reach this
         check with a matching digest: usingPermit2 is not signed, but
         flipping it routes the signature to Permit2's witness digest,
         which differs from the settlement's (InvalidHash), and the auction
         grants Permit2 no allowance anyway.
    @param _hash Order digest the settlement is verifying.
    @param _signature The abi-encoded JamOrder, prefix already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: jam.JamOrder = abi_decode(_signature, jam.JamOrder)
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the order.
    assert abi_encode(order) == _signature, jam.OrderNotValid(reason="NonCanonical")

    assert jam._order_digest(order, self.domain_separator) == _hash, (
        jam.OrderNotValid(reason="InvalidHash")
    )
    assert order.taker == msg.sender, jam.OrderNotValid(reason="BadTaker")
    assert order.hooksHash == jam.EMPTY_HOOKS_HASH, jam.OrderNotValid(reason="BadHooks")
    assert (
        len(order.sellTokens) == 1
        and len(order.sellAmounts) == 1
        and len(order.buyTokens) == 1
        and len(order.buyAmounts) == 1
    ), jam.OrderNotValid(reason="BadTokenCount")
    assert order.nonce != 0, jam.OrderNotValid(reason="ZeroNonce")
    jam._unpack_partner_info(order.partnerInfo)

    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure. The
    # native-coin placeholder is never a lot (LotInactive) nor the payment
    # token (BadBuyToken).
    assert staticcall IDutchAuction(msg.sender).check_order(
        order.sellTokens[0],
        order.buyTokens[0],
        order.receiver,
        order.sellAmounts[0],
        order.buyAmounts[0],
        order.expiry,
    )
    return c.ERC1271_MAGIC_VALUE
