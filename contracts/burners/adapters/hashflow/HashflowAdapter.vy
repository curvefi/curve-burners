# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title HashflowAdapter
@author Curve Finance
@license MIT
@notice Standalone Hashflow RFQ-m settlement adapter for Dutch auctions:
        validates the quote digest HashflowRouter asks the auction (the
        RFQ-m trader) to sign, forwarded by the auction's prefix router,
        against the quote it encodes and the auction's shared economic order
        check, and builds the trader side of a quote request for keepers.
@dev Registry conventions: adapter = this contract, executor = the
     HashflowRouter: a fill pulls baseTokenAmount from the trader with a
     plain transferFrom by the router (no Permit2), so the router is what
     auctions approve. Signature template: the relayer submits
     tradeRFQM(quote) with takerSignature = this_adapter ++
     abi.encode(Quote) where Quote is the nine signed fields (the router's
     EIP-712 type); the router verifies it through OpenZeppelin's
     SignatureChecker, which reaches the trader's (the auction's)
     isValidSignature with the EIP-712 digest. Every RFQ-m quote is an
     exact fill: the signed baseTokenAmount is pulled in full and the
     signed quoteTokenAmount is paid in full to the trader — the protocol
     has no receiver field and no partial fills, no fee fields, no hooks —
     and the router burns the txid on the fill, so a quote never settles
     twice. The trader being the paid account, the economic check runs
     with the auction itself as receiver: the auction's proceeds stay on
     it until push_target forwards them (see the README).
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner): the router checks the contract
             signature on every fill, so no validated-but-unfilled quote
             survives a disable. sync_executor_approvals(router, tokens)
             completes the emergency procedure as hygiene.
@custom:security Holds no funds and receives no allowances (the executor —
                 the router — does, and spends them only under a digest this
                 adapter validated through the auction's router).
                 Auction-agnostic: every economic decision is delegated to
                 msg.sender's check_order, so calling this contract directly
                 proves nothing about any auction. Validity here means the
                 auction accepts the fill; protocol-side liveness (pool
                 authorization, txid, the maker's signature) is the
                 relayer's concern and cannot be read from a signature
                 check. The router swallows every revert of the signature
                 check (SignatureChecker staticcalls and reports any
                 failure as its own "Invalid signer." string), so a typed
                 reason reaches a publisher only through an eth_call of the
                 auction's isValidSignature, not through a failed fill.
"""

from contracts.burners.adapters.hashflow import hashflow
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadRouter:
    pass


error BadDomain:
    pass


error NothingToSell:
    pass


interface Router:
    def eip712Domain() -> (
        bytes1, String[64], String[64], uint256, address, bytes32, DynArray[uint256, 8]
    ): view


ADAPTER_VERSION: public(constant(String[20])) = "HashflowAdapter"
# `this_adapter ++ abi_encode(Quote)`: the 20-byte adapter address the
# auction's router strips, then the bare quote this contract validates.
SIGNATURE_LEN: constant(uint256) = hashflow.MAX_TAKER_SIGNATURE_LEN

# Protocol wiring pinned at deploy: the EIP-712 domain is fixed by the
# router's name, version and address (the router is not upgradeable), so a
# new router means a fresh adapter deployment and a new registry entry
# anyway.
router: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_router: address):
    """
    @notice Pin the router and its EIP-712 domain.
    @dev The router's eip712Domain() (ERC-5267) must report the fixed name
         and version, this chain, the router itself as verifying contract
         and nothing else, so a wrong address, a fork with another domain
         or another protocol version fails the deployment; the domain
         separator is rebuilt from those fields.
    @param _router HashflowRouter of this chain.
    """
    assert _router != empty(address), BadRouter()
    fields: bytes1 = empty(bytes1)
    name: String[64] = ""
    version: String[64] = ""
    chain_id: uint256 = 0
    verifying_contract: address = empty(address)
    salt: bytes32 = empty(bytes32)
    extensions: DynArray[uint256, 8] = []
    fields, name, version, chain_id, verifying_contract, salt, extensions = staticcall Router(
        _router
    ).eip712Domain()
    assert (
        fields == hashflow.DOMAIN_FIELDS
        and name == hashflow.DOMAIN_NAME
        and version == hashflow.DOMAIN_VERSION
        and chain_id == chain.id
        and verifying_contract == _router
        and salt == empty(bytes32)
        and len(extensions) == 0
    ), BadDomain()

    self.router = _router
    self.domain_separator = hashflow._domain_separator(chain.id, _router)


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _sell_amount: uint256 = 0
) -> hashflow.Quote:
    """
    @notice Build the trader side of an RFQ-m quote request for a live lot.
    @dev Priced at the current block: quoteTokenAmount is the auction's quote
         now, the least a market maker's quote may pay, and quoteExpiry is
         the lot end, the latest expiry the auction accepts; the market
         maker fills pool, externalAccount and txid, sets its own (shorter)
         quoteExpiry and quoteTokenAmount, and signs. The keeper checks the
         returned quote against the curve (an eth_call of the auction's
         isValidSignature on its digest, see describe), wraps it with
         signature_for and submits tradeRFQM, or hands it to any relayer.
         Exact fill by protocol design: the whole baseTokenAmount against
         exactly quoteTokenAmount. Nothing here is a standing ask: a quote
         lives only until its quoteExpiry.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _sell_amount Amount to sell; 0 sells everything available.
    @return The quote template: trader, tokens, amounts and expiry set, the
            maker's fields (pool, externalAccount, txid) zero.
    """
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = staticcall _auction.available(_token)
    assert sell_amount > 0, NothingToSell()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)

    return hashflow.Quote(
        txid=empty(bytes32),
        trader=_auction.address,
        pool=empty(address),
        externalAccount=empty(address),
        baseToken=_token,
        quoteToken=(staticcall _auction.want()).address,
        baseTokenAmount=sell_amount,
        quoteTokenAmount=staticcall _auction.getAmountNeeded(_token, sell_amount),
        quoteExpiry=lot_end,
    )


@external
@view
def signature_for(_quote: hashflow.Quote) -> Bytes[SIGNATURE_LEN]:
    """
    @notice The takerSignature for a complete quote: `this_adapter ++
            abi_encode(quote)`.
    @param _quote The quote as the market maker returned it.
    @return The bytes to submit as RFQMQuote.takerSignature.
    """
    return concat(convert(self, bytes20), abi_encode(_quote))


@external
@view
def describe(_payload: Bytes[hashflow.ENCODED_QUOTE_LEN]) -> (hashflow.Quote, bytes32):
    """
    @notice Decode a signature payload and the digest the router will ask the
            trader about under this adapter's domain.
    @param _payload The abi-encoded Quote (a takerSignature without its
           adapter prefix).
    @return The quote and its EIP-712 digest.
    """
    assert len(_payload) == hashflow.ENCODED_QUOTE_LEN, hashflow.OrderNotValid(
        reason="NonCanonical"
    )
    quote: hashflow.Quote = abi_decode(_payload, hashflow.Quote)
    return quote, hashflow._quote_digest(quote, self.domain_separator)


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[hashflow.ENCODED_QUOTE_LEN]) -> bytes4:
    """
    @notice Validate an RFQ-m quote digest against the calling auction's live
            lot economics.
    @dev The payload is the bare abi-encoded Quote (the router already
         stripped the adapter prefix). Protocol checks revert with
         hashflow.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The
         calling router lets every revert bubble. A shorter payload is
         NonCanonical (every field is static, so one length is the one
         encoding); a longer one fails ABI decoding before any check runs
         and bubbles up the same way. Shape: the calling auction as trader, the
         account the router debits and the pool credits. Pass-through
         fields: pool and externalAccount only decide which market-maker
         account pays (the pool itself or its external account, each paying
         the full quoteTokenAmount), txid is the router's replay key. The
         quote's amounts are priced as a whole by check_order with the
         auction itself as receiver and quoteExpiry as the validity bound
         (inclusive on both sides, like the router's own expiry check). The
         native-coin placeholder (address zero) is never a lot
         (LotInactive) nor the payment token (BadBuyToken), and the router
         rejects it as baseToken anyway.
    @param _hash Quote digest the router is verifying.
    @param _signature The abi-encoded Quote, prefix already stripped.
    @return ERC-1271 magic value for a fillable quote (anything else reverts).
    """
    assert len(_signature) == hashflow.ENCODED_QUOTE_LEN, (
        hashflow.OrderNotValid(reason="NonCanonical")
    )
    quote: hashflow.Quote = abi_decode(_signature, hashflow.Quote)

    assert hashflow._quote_digest(quote, self.domain_separator) == _hash, (
        hashflow.OrderNotValid(reason="InvalidHash")
    )
    assert quote.trader == msg.sender, hashflow.OrderNotValid(reason="BadTrader")

    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure. The
    # trader is the paid account, so the receiver is the auction itself.
    assert staticcall IDutchAuction(msg.sender).check_order(
        quote.baseToken,
        quote.quoteToken,
        msg.sender,
        quote.baseTokenAmount,
        quote.quoteTokenAmount,
        quote.quoteExpiry,
    )
    return c.ERC1271_MAGIC_VALUE
