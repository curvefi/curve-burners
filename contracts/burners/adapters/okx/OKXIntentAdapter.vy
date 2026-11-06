# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OKXIntentAdapter
@author Curve Finance
@license MIT
@notice Standalone OKX Intent Swap settlement adapter for Dutch auctions:
        validates the order digest the OKX Settlement asks the auction (the
        order owner) to sign, forwarded by the auction's prefix router,
        against the order it encodes and the auction's shared economic order
        check, and builds publishable orders for keepers.
@dev Registry conventions: adapter = this contract, executor = the OKX DEX
     TokenApprove read through the settlement's tokenApproveProxy at
     deploy: a fill pulls the sold tokens with a plain transferFrom from
     the owner through that contract, so it is what auctions approve (the
     settlement and its proxy never call transferFrom themselves).
     Signature template: an allowlisted solver settles a trade with
     signature = auction ++ this_adapter ++ abi.encode(Order) and the
     EIP-1271 scheme in its flags; the settlement strips the owner and
     forwards the rest verbatim to the owner's (the auction's)
     isValidSignature with the EIP-712 digest. The API path posts
     `this_adapter ++ abi.encode(Order)` with signingScheme eip1271 (see
     order_for and isValidSignature). Every order is fill-or-kill by
     protocol design: the settlement rejects the partial-fill bit, pulls the
     signed fromTokenAmount in full and pays the receiver at least the
     signed toTokenAmount after every commission and solver fee, and the
     order uid is filled once.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner): the settlement checks the contract
             signature on every fill, so no validated-but-unfilled order
             survives a disable. sync_executor_approvals(token_approve,
             tokens) completes the emergency procedure as hygiene.
@custom:security Holds no funds and receives no allowances (the executor —
                 OKX's TokenApprove — does, and spends them for any contract
                 its proxy allowlists, the settlement among them; listing
                 this adapter is the owner's review of that trust).
                 Auction-agnostic: every economic decision is delegated to
                 msg.sender's check_order, so calling this contract directly
                 proves nothing about any auction. Validity here means the
                 auction accepts the fill; protocol-side liveness (uid filled
                 or invalidated, solver allowlist) is the publisher's concern
                 and cannot be read from a signature check. The settlement
                 lets a revert of the signature check bubble unchanged (a
                 typed reason reaches the solver) and reports a non-magic
                 answer, such as the router's 0xffffffff for a disabled
                 adapter, as its own "invalid eip1271 signature".
"""

from contracts.burners.adapters.okx import intent_swap
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadSettlement:
    pass


error BadDomain:
    pass


error BadTokenApprove:
    pass


error NothingToSell:
    pass


error BadExpiration:
    pass


interface Settlement:
    def domainSeparator() -> bytes32: view
    def tokenApproveProxy() -> address: view


interface TokenApproveProxy:
    def tokenApprove() -> address: view


ADAPTER_VERSION: public(constant(String[20])) = "OKXIntentAdapter"
# `this_adapter ++ abi_encode(Order)`: the 20-byte adapter address the
# auction's router strips, then the bare order this contract validates. A
# published order carries no commissions (intent_swap.ENCODED_PLAIN_ORDER_LEN
# bytes); the bound is the encoder's.
SIGNATURE_BOUND: constant(uint256) = 20 + intent_swap.ENCODED_ORDER_BOUND
# validTo is a uint32 on the protocol side.
MAX_VALID_TO: constant(uint256) = 2**32 - 1

# Protocol wiring pinned at deploy: the EIP-712 domain is fixed by the
# settlement's name, version and address and the approval target is
# immutable in the settlement, so a settlement upgrade means a fresh adapter
# deployment and a new registry entry anyway.
settlement: public(immutable(address))
domain_separator: public(immutable(bytes32))
token_approve_proxy: public(immutable(address))
token_approve: public(immutable(address))


@deploy
def __init__(_settlement: address):
    """
    @notice Pin the settlement, its EIP-712 domain and its approval target.
    @dev The domain is rebuilt from the fixed name and version, this chain
         and the settlement address and checked against the settlement's
         domainSeparator, so a wrong address, a proxy or fork with another
         domain or another protocol version fails the deployment. The
         approval target is read through the settlement's immutable
         tokenApproveProxy (TokenApproveProxy.tokenApprove), the contract
         whose claimTokens pulls from order owners.
    @param _settlement OKX Intent Swap Settlement of this chain.
    """
    assert _settlement != empty(address), BadSettlement()
    protocol: Settlement = Settlement(_settlement)
    domain: bytes32 = intent_swap._domain_separator(chain.id, _settlement)
    assert domain == staticcall protocol.domainSeparator(), BadDomain()
    proxy: address = staticcall protocol.tokenApproveProxy()
    assert proxy != empty(address), BadTokenApprove()
    approve_target: address = staticcall TokenApproveProxy(proxy).tokenApprove()
    assert approve_target != empty(address), BadTokenApprove()

    self.settlement = _settlement
    self.domain_separator = domain
    self.token_approve_proxy = proxy
    self.token_approve = approve_target


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction,
    _token: address,
    _sell_amount: uint256 = 0,
    _app_data: bytes32 = empty(bytes32),
) -> (intent_swap.Order, Bytes[SIGNATURE_BOUND]):
    """
    @notice Build the order a publisher hands to OKX solvers for a live lot,
            together with its signature for the auction.
    @dev Priced at the current block: the buy amount is the auction's quote
         now, so the order stays valid while the curve decays below it (a
         standing ask) and a publisher re-posts, at the lower price, as the
         curve falls (the settlement keys fills by the order uid, the digest
         with owner and validTo, so a re-post with different amounts is a
         new order; equal fields need a different appData). Fill-or-kill by
         protocol design: the whole sell amount against at least the buy
         amount. Validity ends with the lot window (the settlement's validTo
         is inclusive; check_order rejects at lot_end anyway). No
         commissions. The API path builds its own order through the quote
         (commissions and appData set by OKX); this one is for a direct
         solver or a re-post of the same shape.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _sell_amount Amount to sell; 0 sells everything available.
    @param _app_data appData of the order; free (no on-chain effect), so it
           doubles as a salt.
    @return The order and `this_adapter ++ abi_encode(order)`.
    """
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = staticcall _auction.available(_token)
    assert sell_amount > 0, NothingToSell()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)
    assert lot_end <= MAX_VALID_TO, BadExpiration()

    order: intent_swap.Order = intent_swap.Order(
        fromTokenAddress=_token,
        toTokenAddress=(staticcall _auction.want()).address,
        owner=_auction.address,
        receiver=staticcall _auction.receiver(),
        fromTokenAmount=sell_amount,
        toTokenAmount=staticcall _auction.getAmountNeeded(_token, sell_amount),
        validTo=convert(lot_end, uint32),
        appData=_app_data,
        swapMode=intent_swap.EXACT_IN,
        partiallyFillable=False,
        commissionInfos=[],
    )
    return order, concat(convert(self, bytes20), abi_encode(order))


@external
@view
def describe(
    _payload: Bytes[intent_swap.ENCODED_ORDER_BOUND],
) -> (intent_swap.Order, bytes32, Bytes[intent_swap.UID_LENGTH]):
    """
    @notice Decode a signature payload (the bytes after the adapter prefix)
            into its order, the digest under this settlement's domain, and
            the order uid the settlement keys filledAmount and
            invalidateOrder by.
    @param _payload abi_encode(Order), the signature without its prefix.
    @return The order, its EIP-712 digest and its 56-byte uid.
    """
    order: intent_swap.Order = abi_decode(_payload, intent_swap.Order)
    digest: bytes32 = intent_swap._order_digest(order, self.domain_separator)
    return order, digest, intent_swap._order_uid(digest, order.owner, order.validTo)


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[intent_swap.ENCODED_ORDER_BOUND]) -> bytes4:
    """
    @notice Validate an OKX Intent Swap order digest against the calling
            auction's live lot economics.
    @dev The payload is the abi-encoded Order (the router already stripped
         the adapter prefix; the settlement stripped the owner before it).
         Protocol checks revert with intent_swap.OrderNotValid(reason); the
         economic check is the auction's check_order, whose typed reverts
         pass through untouched. The calling router lets every revert
         bubble. A payload that does not decode as one order (truncation,
         more than MAX_COMMISSIONS entries) fails ABI decoding before any
         check runs and bubbles up the same way; one that decodes but is
         not the order's canonical encoding is NonCanonical. Shape: the
         calling auction as owner (the settlement requires the signed owner
         to be the prefix owner), the exactIn swap mode and no partial-fill
         flag (the only shape the settlement hashes and fills, and the one
         that makes the signed amounts the whole fill), commissions the
         settlement accepts (every fee, commission or solver, comes out
         above the signed toTokenAmount; "ToB" fees are paid by the
         settlement). Pass-through fields: appData (no on-chain effect) and
         the commissions themselves. The order's amounts are priced as a
         whole by check_order with the receiver resolved like the protocol
         resolves it (empty means the owner, which the auction rejects) and
         validTo as the validity bound.
    @param _hash Order digest the settlement is verifying.
    @param _signature The abi-encoded Order, prefix already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: intent_swap.Order = abi_decode(_signature, intent_swap.Order)
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the order.
    assert abi_encode(order) == _signature, intent_swap.OrderNotValid(reason="NonCanonical")

    assert intent_swap._order_digest(order, self.domain_separator) == _hash, (
        intent_swap.OrderNotValid(reason="InvalidHash")
    )
    assert order.owner == msg.sender, intent_swap.OrderNotValid(reason="BadOwner")
    assert order.swapMode == intent_swap.EXACT_IN and not order.partiallyFillable, (
        intent_swap.OrderNotValid(reason="BadOrderFlags")
    )
    intent_swap._check_commissions(order.commissionInfos)

    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure. The
    # native-coin placeholder is never the payment token (BadBuyToken) and
    # the settlement never pulls it.
    assert staticcall IDutchAuction(msg.sender).check_order(
        order.fromTokenAddress,
        order.toTokenAddress,
        intent_swap._receiver(order),
        order.fromTokenAmount,
        order.toTokenAmount,
        convert(order.validTo, uint256),
    )
    return c.ERC1271_MAGIC_VALUE
