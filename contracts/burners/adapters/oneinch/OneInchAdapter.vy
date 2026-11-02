# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OneInchAdapter
@author Curve Finance
@license MIT
@notice Standalone 1inch Limit Order Protocol v4 settlement adapter for Dutch
        auctions: validates the order digest the protocol asks the auction to
        sign, forwarded by the auction's prefix router, against the order (and
        extension) it encodes and the auction's shared economic order check,
        and builds publishable orders for takers.
@dev Registry conventions: adapter = this contract, executor = the protocol
     (AggregationRouterV6, which hosts LOP v4): a fill pulls the maker asset
     with transferFrom from the maker, so the protocol is what auctions
     approve. Signature template: a taker calls
     fillContractOrder[Args](order, sig, ...) with sig = this_adapter ++
     abi.encode(Order, extension) (the extension is empty for the plain
     orders order_for builds); the protocol forwards sig verbatim to the
     maker's (the auction's) isValidSignature (see order_for and
     isValidSignature). Linear pricing only: extensions with amount getters
     (Fusion settlement, FeeTaker) are rejected, so the taking amount is a
     fixed ratio of the making amount, rounded in the maker's favour, and
     the order's takingAmount is a floor on what any fill pays per unit.
     The protocol checks a contract signature on the first fill of an order
     only: a partially filled standing ask keeps filling at its signed price
     (always at or above the curve at validation time, which only decays)
     until it expires, without asking the auction again.
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
                 amount, invalidators, epochs, predicates, allowed sender)
                 is the publisher's concern and cannot be read from a
                 signature check. The protocol reports any revert of the
                 signature check as its own BadSignature, so a publisher
                 classifies an order by eth_call against the auction's
                 isValidSignature, not against a fill.
"""

from contracts.burners.adapters.oneinch import oneinch
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadProtocol:
    pass


error BadDomain:
    pass


error NothingToSell:
    pass


error BadExpiration:
    pass


interface LimitOrderProtocol:
    def eip712Domain() -> (
        bytes1,
        String[oneinch.MAX_DOMAIN_NAME],
        String[oneinch.MAX_DOMAIN_VERSION],
        uint256,
        address,
        bytes32,
        DynArray[uint256, MAX_DOMAIN_EXTENSIONS],
    ): view
    def hashOrder(_order: oneinch.OrderWords) -> bytes32: view


ADAPTER_VERSION: public(constant(String[20])) = "OneInchAdapter"
# `this_adapter ++ abi_encode(Order, extension)`: the 20-byte adapter
# address the auction's router strips, then the bare order and its
# extension this contract validates. abi_encode of the pair at its bound:
# eight order words, the extension offset and length words, the padded
# extension bytes.
PAYLOAD_BOUND: constant(uint256) = oneinch.ENCODED_ORDER_LEN + 2 * 32 + oneinch.MAX_EXTENSION
# A published order carries an empty extension: eight words, offset, length.
SIGNATURE_LEN: constant(uint256) = 20 + oneinch.ENCODED_ORDER_LEN + 2 * 32
# ERC-5267 extensions list bound; the protocol reports none.
MAX_DOMAIN_EXTENSIONS: constant(uint256) = 8
# The maker traits of a published order: partial and multiple fills (a
# standing ask filled in pieces), no extension, direct transferFrom, no
# unwrapping, any taker; the expiration is added per order.
PUBLISHED_TRAITS: constant(uint256) = oneinch.ALLOW_MULTIPLE_FILLS_FLAG

# Protocol wiring pinned at deploy: the EIP-712 domain is fixed by the
# protocol's name, version and address, so a protocol upgrade means a fresh
# adapter deployment and a new registry entry anyway.
limit_order_protocol: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_limit_order_protocol: address):
    """
    @notice Pin the protocol and its EIP-712 domain.
    @dev The domain is rebuilt from eip712Domain() (ERC-5267) and checked
         against the protocol's own hashOrder on a probe order, so a wrong
         address, a proxy with another domain or a different order type
         fails the deployment.
    @param _limit_order_protocol Limit Order Protocol v4 of this chain
           (AggregationRouterV6).
    """
    assert _limit_order_protocol != empty(address), BadProtocol()
    protocol: LimitOrderProtocol = LimitOrderProtocol(_limit_order_protocol)
    fields: bytes1 = empty(bytes1)
    name: String[oneinch.MAX_DOMAIN_NAME] = ""
    version: String[oneinch.MAX_DOMAIN_VERSION] = ""
    chain_id: uint256 = 0
    verifying_contract: address = empty(address)
    salt: bytes32 = empty(bytes32)
    extensions: DynArray[uint256, MAX_DOMAIN_EXTENSIONS] = []
    fields, name, version, chain_id, verifying_contract, salt, extensions = (
        staticcall protocol.eip712Domain()
    )
    assert (
        fields == oneinch.EIP712_DOMAIN_FIELDS
        and chain_id == chain.id
        and verifying_contract == _limit_order_protocol
    ), BadDomain()
    domain: bytes32 = oneinch._domain_separator(name, version, chain_id, verifying_contract)
    probe: oneinch.Order = empty(oneinch.Order)
    probe_hash: bytes32 = staticcall protocol.hashOrder(oneinch._to_words(probe))
    assert probe_hash == oneinch._order_digest(probe, domain), BadDomain()

    self.limit_order_protocol = _limit_order_protocol
    self.domain_separator = domain


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _salt: uint256, _sell_amount: uint256 = 0
) -> (oneinch.Order, Bytes[SIGNATURE_LEN]):
    """
    @notice Build the order a publisher hands to 1inch takers for a live
            lot, together with its signature for the auction.
    @dev Priced at the current block: the taking amount is the auction's
         quote now, so the order stays valid while the curve decays below it
         (a standing ask) and a publisher re-posts, with a fresh salt, as
         the price falls. Partial and multiple fills are allowed; every
         fill pays the same unit price rounded up. Validity ends with the
         lot window. No extension, any taker. Anyone may publish.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _salt Order salt; the protocol tracks fills per order hash, so
           two live orders with equal fields need different salts.
    @param _sell_amount Amount to sell; 0 sells everything available.
    @return The order and `this_adapter ++ abi_encode(order, b"")`.
    """
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = staticcall _auction.available(_token)
    assert sell_amount > 0, NothingToSell()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)
    # The expiration is inclusive on the protocol side; check_order rejects
    # at lot_end anyway (LotInactive), so nothing settles past the window.
    assert lot_end <= oneinch.EXPIRATION_MASK, BadExpiration()

    order: oneinch.Order = oneinch.Order(
        salt=_salt,
        maker=_auction.address,
        receiver=staticcall _auction.receiver(),
        makerAsset=_token,
        takerAsset=(staticcall _auction.want()).address,
        makingAmount=sell_amount,
        takingAmount=staticcall _auction.getAmountNeeded(_token, sell_amount),
        makerTraits=PUBLISHED_TRAITS | (lot_end << oneinch.EXPIRATION_OFFSET),
    )
    return order, concat(convert(self, bytes20), abi_encode(order, b""))


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[PAYLOAD_BOUND]) -> bytes4:
    """
    @notice Validate a 1inch order digest against the calling auction's live
            lot economics.
    @dev The payload is the abi-encoded pair of the bare Order and its
         extension bytes, empty for an order without one (the router already
         stripped the adapter prefix). Protocol checks revert with
         oneinch.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The calling
         router lets every revert bubble. A payload that does not decode as
         one pair (truncation, an extension above the bound) fails ABI
         decoding before any check runs and bubbles up the same way; one
         that decodes but is not the pair's canonical encoding is
         NonCanonical. The extension must be bound to the order the way the
         protocol binds it (flag and salt) and must carry neither asset
         suffixes nor amount getters, so the protocol prices every fill
         linearly from the order amounts; predicates, permits and
         interactions only restrict or decorate a fill and pass through.
         The order's amounts are priced as a whole (partial fills keep the
         unit price), the receiver resolves like the protocol resolves it
         (empty means the maker), and the maker-traits expiration is the
         validity bound (0, never expiring, fails it).
    @param _hash Order digest the protocol is verifying.
    @param _signature The abi-encoded (Order, extension) pair, prefix
           already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: oneinch.Order = empty(oneinch.Order)
    extension: Bytes[oneinch.MAX_EXTENSION] = b""
    order, extension = abi_decode(_signature, (oneinch.Order, Bytes[oneinch.MAX_EXTENSION]))
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the pair.
    assert abi_encode(order, extension) == _signature, oneinch.OrderNotValid(reason="NonCanonical")

    assert oneinch._order_digest(order, self.domain_separator) == _hash, (
        oneinch.OrderNotValid(reason="InvalidHash")
    )
    assert order.maker == msg.sender, oneinch.OrderNotValid(reason="BadMaker")
    # The executor pulls with a plain transferFrom (no Permit2 allowance
    # exists) and the auction's receiver takes the payment token itself.
    assert not (
        oneinch._has_flag(order.makerTraits, oneinch.USE_PERMIT2_FLAG)
        or oneinch._has_flag(order.makerTraits, oneinch.UNWRAP_WETH_FLAG)
    ), oneinch.OrderNotValid(reason="BadMakerTraits")
    oneinch._check_extension_binding(order, extension)
    offsets: uint256 = oneinch._extension_offsets(extension)
    # Suffixes alter the transferFrom calldata; getters replace the linear
    # price. Neither leaves the order amounts in charge of the economics.
    assert (
        oneinch._field_is_empty(offsets, oneinch.MAKER_ASSET_SUFFIX)
        and oneinch._field_is_empty(offsets, oneinch.TAKER_ASSET_SUFFIX)
    ), oneinch.OrderNotValid(reason="BadAssetSuffix")
    assert (
        oneinch._field_is_empty(offsets, oneinch.MAKING_AMOUNT_DATA)
        and oneinch._field_is_empty(offsets, oneinch.TAKING_AMOUNT_DATA)
    ), oneinch.OrderNotValid(reason="BadAmountGetter")

    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure.
    assert staticcall IDutchAuction(msg.sender).check_order(
        order.makerAsset,
        order.takerAsset,
        oneinch._receiver(order),
        order.makingAmount,
        order.takingAmount,
        oneinch._expiration(order.makerTraits),
    )
    return c.ERC1271_MAGIC_VALUE
