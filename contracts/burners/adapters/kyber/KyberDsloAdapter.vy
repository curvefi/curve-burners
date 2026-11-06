# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title KyberDsloAdapter
@author Curve Finance
@license MIT
@notice Standalone KyberSwap DSLO (Double Signature Limit Order) settlement
        adapter for Dutch auctions: validates the order digest the protocol
        asks the auction to sign, forwarded by the auction's prefix router,
        against the order it encodes and the auction's shared economic order
        check, and builds publishable orders for takers.
@dev Registry conventions: adapter = this contract, executor = the protocol
     (DSLOProtocol): a fill pulls the maker asset with a plain transferFrom
     from the maker, so the protocol is what auctions approve. Signature
     template: a taker calls fillOrderTo(params, amounts, sender) with
     params.signature.orderSignature = this_adapter ++ abi.encode(Order);
     the protocol forwards those bytes verbatim to the maker's (the
     auction's) isValidSignature with the order digest (see order_for and
     isValidSignature). The second signature of the name — the operator
     co-signature every fill carries, issued per fill by the KyberSwap API —
     is outside the maker's signature and this adapter's concern: it gates
     who fills, never what the maker receives. Linear pricing only: amount
     getters are pinned to the protocol's own linear calculators over the
     order amounts (or empty: exact-amount fills), so the taking amount is a
     fixed ratio of the making amount, rounded in the maker's favour, and
     the order's takingAmount is a floor on what any fill pays per unit; the
     feeConfig fee is either paid by the taker on top or carved out of the
     taker's maker-asset receipt, never out of the maker's taking amount.
     The protocol checks a contract signature on the first fill of an order
     only: a partially filled standing ask keeps filling at its signed price
     (always at or above the curve at validation time, which only decays)
     until its predicate expires, without asking the auction again.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner); native take() stays untouched.
             Because the protocol does not re-check partially filled orders,
             an emergency disable is complete only with
             sync_executor_approvals(protocol, tokens), which zeroes the
             allowance every remaining fill relies on.
@custom:security Holds no funds and receives no allowances (the executor —
                 the protocol — does, and spends them only under a digest
                 this adapter validated through the auction's router, or for
                 the remainder of an order it validated earlier).
                 Auction-agnostic: every economic decision is delegated to
                 msg.sender's check_order, so calling this contract directly
                 proves nothing about any auction. Validity here means the
                 auction accepts the fill; protocol-side liveness (remaining
                 amount, cancellation, nonce, pause, operator co-signature,
                 allowed sender) is the publisher's concern and cannot be
                 read from a signature check. The protocol reports any
                 failed signature check as its own 'LOP: bad signature', so
                 a publisher classifies an order by eth_call against the
                 auction's isValidSignature, not against a fill.
"""

from contracts.burners.adapters.kyber import dslo
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadProtocol:
    pass


error BadDomain:
    pass


error NothingToSell:
    pass


interface DsloProtocol:
    def DOMAIN_SEPARATOR() -> bytes32: view
    def hashOrder(_order: dslo.Order) -> bytes32: view


ADAPTER_VERSION: public(constant(String[20])) = "KyberDsloAdapter"
# `this_adapter ++ abi_encode(Order)`: the 20-byte adapter address the
# auction's router strips, then the bare order this contract validates.
SIGNATURE_BOUND: constant(uint256) = 20 + dslo.ENCODED_ORDER_BOUND

# Protocol wiring pinned at deploy: the EIP-712 domain is fixed by the
# protocol's name, version and address, so a protocol upgrade means a fresh
# adapter deployment and a new registry entry anyway.
protocol: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_protocol: address):
    """
    @notice Pin the protocol and its EIP-712 domain.
    @dev The domain is rebuilt from the protocol's fixed name and version
         for this chain and address and checked against the protocol's own
         DOMAIN_SEPARATOR, then the order hashing against its hashOrder on
         a probe order, so a wrong address, a different domain or a
         different order type fails the deployment.
    @param _protocol DSLOProtocol of this chain.
    """
    assert _protocol != empty(address), BadProtocol()
    domain: bytes32 = staticcall DsloProtocol(_protocol).DOMAIN_SEPARATOR()
    assert domain == dslo._domain_separator(chain.id, _protocol), BadDomain()
    probe: dslo.Order = empty(dslo.Order)
    probe_hash: bytes32 = staticcall DsloProtocol(_protocol).hashOrder(probe)
    assert probe_hash == dslo._order_digest(probe, domain), BadDomain()

    self.protocol = _protocol
    self.domain_separator = domain


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _salt: uint256, _sell_amount: uint256 = 0
) -> (dslo.Order, Bytes[SIGNATURE_BOUND]):
    """
    @notice Build the order a publisher hands to DSLO takers for a live
            lot, together with its signature for the auction.
    @dev Priced at the current block: the taking amount is the auction's
         quote now, so the order stays valid while the curve decays below it
         (a standing ask) and a publisher re-posts, with a fresh salt, as
         the price falls. Partial fills are allowed through the linear
         getters; every fill pays the same unit price rounded up. The
         predicate `timestampBelow(lot_end + 1)` ends validity with the lot
         window. No fee, any taker, no asset data, no interaction. An order
         placed through the KyberSwap API instead carries the fields the
         API's sign-message returns (its salt, feeConfig and
         `and(nonceEquals, timestampBelow)` predicate); this adapter accepts
         that shape too, and the signature is built the same way from it.
         Anyone may publish.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _salt Order salt; the protocol tracks fills per order hash, so
           two live orders with equal fields need different salts.
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
    taking_amount: uint256 = staticcall _auction.getAmountNeeded(_token, sell_amount)

    order: dslo.Order = dslo.Order(
        salt=_salt,
        makerAsset=_token,
        takerAsset=(staticcall _auction.want()).address,
        maker=_auction.address,
        receiver=staticcall _auction.receiver(),
        allowedSender=empty(address),
        makingAmount=sell_amount,
        takingAmount=taking_amount,
        feeConfig=0,
        makerAssetData=b"",
        takerAssetData=b"",
        getMakerAmount=b"",
        getTakerAmount=b"",
        # timestampBelow is strict, so the last fillable second is lot_end;
        # check_order rejects at lot_end anyway (LotInactive).
        predicate=dslo._timestamp_below(lot_end + 1),
        interaction=b"",
    )
    order.getMakerAmount = dslo._linear_getter(dslo.GET_MAKER_AMOUNT_SELECTOR, order)
    order.getTakerAmount = dslo._linear_getter(dslo.GET_TAKER_AMOUNT_SELECTOR, order)
    return order, concat(convert(self, bytes20), abi_encode(order))


@external
@view
def describe(_order: dslo.Order) -> (address, address, uint256, bool, uint256):
    """
    @notice Unpack the derived fields of an order the way the protocol and
            this adapter read them.
    @param _order The order.
    @return Resolved taker-asset receiver, fee recipient, fee percent (1e-5
            units), whether the fee is charged on the taker asset, and the
            last timestamp the predicate lets the order fill at (reverts
            with BadPredicate for a predicate shape this adapter rejects).
    """
    fee_recipient: address = empty(address)
    fee_percent: uint256 = 0
    taker_asset_fee: bool = False
    fee_recipient, fee_percent, taker_asset_fee = dslo._fee_config(_order.feeConfig)
    return (
        dslo._receiver(_order),
        fee_recipient,
        fee_percent,
        taker_asset_fee,
        dslo._valid_to(_order, self.protocol),
    )


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[dslo.ENCODED_ORDER_BOUND]) -> bytes4:
    """
    @notice Validate a DSLO order digest against the calling auction's live
            lot economics.
    @dev The payload is the abi-encoded bare Order (the router already
         stripped the adapter prefix). Protocol checks revert with
         dslo.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The
         calling router lets every revert bubble. A payload that does not
         decode as one order (truncation, a field above its bound) fails ABI
         decoding before any check runs and bubbles up the same way; one
         that decodes but is not the order's canonical encoding is
         NonCanonical. Shape: the calling auction as maker, no asset data
         (it is appended to the transferFrom calldata), getters empty or
         the linear ones over the order's own amounts, no interaction, and a
         predicate carrying the expiry (`timestampBelow`, alone or inside
         the API's `and` with `nonceEquals(maker, nonce)` entries).
         Pass-through fields: salt, allowedSender (only restricts who fills)
         and feeConfig (the fee never lowers the maker's taking amount). The
         order's amounts are priced as a whole (partial fills keep the unit
         price), the receiver resolves like the protocol resolves it (empty
         means the maker), and the predicate's expiry is the validity bound.
    @param _hash Order digest the protocol is verifying.
    @param _signature The abi-encoded Order, prefix already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: dslo.Order = abi_decode(_signature, dslo.Order)
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the order.
    assert abi_encode(order) == _signature, dslo.OrderNotValid(reason="NonCanonical")

    assert dslo._order_digest(order, self.domain_separator) == _hash, (
        dslo.OrderNotValid(reason="InvalidHash")
    )
    assert order.maker == msg.sender, dslo.OrderNotValid(reason="BadMaker")
    assert len(order.makerAssetData) == 0 and len(order.takerAssetData) == 0, (
        dslo.OrderNotValid(reason="BadAssetData")
    )
    dslo._check_getters(order)
    assert len(order.interaction) == 0, dslo.OrderNotValid(reason="BadInteraction")
    valid_to: uint256 = dslo._valid_to(order, self.protocol)

    # The shared economic check: the calling auction prices the order as a
    # whole against its live curve and reverts with its own typed error on
    # failure.
    assert staticcall IDutchAuction(msg.sender).check_order(
        order.makerAsset,
        order.takerAsset,
        dslo._receiver(order),
        order.makingAmount,
        order.takingAmount,
        valid_to,
    )
    return c.ERC1271_MAGIC_VALUE
