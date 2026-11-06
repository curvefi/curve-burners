# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OifEscrowAdapter
@author Curve Finance
@license MIT
@notice Standalone Open Intents Framework (escrow input settler, as deployed
        by LI.FI for LI.FI Intents) settlement adapter for Dutch auctions:
        validates the Permit2 witness digest the escrow asks the auction (the
        order sponsor) to sign, forwarded by the auction's prefix router,
        against the same-chain intent it encodes and the auction's shared
        economic order check, and builds publishable intents for solvers.
@dev Registry conventions: adapter = this contract, executor = Permit2: the
     escrow opens a sponsored intent through
     Permit2.permitWitnessTransferFrom, so Permit2 is what pulls the sold
     tokens and what auctions approve. Signature template: the escrow is
     called with openFor(order, sponsor = auction, signature = 0x00 ++
     this_adapter ++ abi.encode(StandardOrder)) or openForAndFinalise with
     the same bytes; the escrow strips its signature-type byte and Permit2
     forwards the rest verbatim to the sponsor's (the auction's)
     isValidSignature (see order_for and isValidSignature). Intents are
     same-chain by construction: the single output must be delivered on
     this chain by the pinned output settler, which is also the order's
     input oracle, so the escrow releases the input only against that
     settler's own fill attestation (a fill pays the signed amount to the
     recipient). Orders are fill-or-kill: Permit2 burns the nonce on open,
     the output settler pays the signed amount in one fill, and an opened
     but unfilled intent is refundable to the auction after it expires.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner); native take() stays untouched. An
             intent already opened holds its input in the escrow until it
             is filled or refunded (refund is permissionless after expiry).
@custom:security Holds no funds and receives no allowances (the executor —
                 Permit2 — does, and spends them only under a digest this
                 adapter validated through the auction's router).
                 Auction-agnostic: every economic decision is delegated to
                 msg.sender's check_order, so calling this contract directly
                 proves nothing about any auction. The input oracle and the
                 output settler are pinned because they decide when the
                 escrow releases the input: an order naming another oracle
                 could be claimed without any fill. Validity here means the
                 auction accepts the open; escrow-side liveness (an unused
                 nonce, the order status) is the publisher's concern and
                 cannot be read from a signature check. Permit2 lets a
                 revert of the signature check bubble unchanged (a typed
                 reason reaches the solver) and reports a non-magic answer,
                 such as the router's 0xffffffff for a disabled adapter, as
                 its own InvalidContractSignature.
"""

from contracts.burners.adapters.oif import oif
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadSettler:
    pass


error BadOutputSettler:
    pass


error BadPermit2:
    pass


error BadDomainSeparator:
    pass


error NothingToSell:
    pass


error BadFillDeadline:
    pass


interface Permit2:
    def DOMAIN_SEPARATOR() -> bytes32: view


ADAPTER_VERSION: public(constant(String[20])) = "OifEscrowAdapter"
# `0x00 ++ this_adapter ++ abi_encode(StandardOrder)`: the escrow's Permit2
# signature-type byte, the 20-byte adapter address the auction's router
# strips, then the bare order this contract validates.
SIGNATURE_TYPE_PERMIT2: constant(bytes1) = 0x00
SIGNATURE_BOUND: constant(uint256) = 1 + 20 + oif.ENCODED_ORDER_BOUND

# Protocol wiring pinned at deploy: the escrow is the Permit2 spender, the
# output settler is the only oracle whose attestation may release the
# input, Permit2 is the signer's verifying contract. An upgrade of any of
# them means a fresh adapter deployment and a new registry entry.
settler: public(immutable(address))
output_settler: public(immutable(address))
permit2: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_settler: address, _output_settler: address, _permit2: address):
    """
    @notice Pin the escrow, the same-chain output settler, and Permit2 with
            its EIP-712 domain.
    @param _settler OIF escrow input settler of this chain (InputSettlerEscrow
           or InputSettlerEscrowLIFI).
    @param _output_settler Output settler of this chain that fills and
           attests (OutputSettlerSimple); every intent's input oracle.
    @param _permit2 The Permit2 the escrow pulls through.
    """
    assert _settler != empty(address), BadSettler()
    assert _output_settler != empty(address), BadOutputSettler()
    assert _permit2 != empty(address), BadPermit2()
    domain: bytes32 = staticcall Permit2(_permit2).DOMAIN_SEPARATOR()
    assert domain != empty(bytes32), BadDomainSeparator()

    self.settler = _settler
    self.output_settler = _output_settler
    self.permit2 = _permit2
    self.domain_separator = domain


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction,
    _token: address,
    _nonce: uint256,
    _sell_amount: uint256 = 0,
    _fill_deadline: uint256 = 0,
) -> (oif.StandardOrder, Bytes[SIGNATURE_BOUND]):
    """
    @notice Build the intent a publisher hands to solvers for a live lot,
            together with the escrow signature for the auction.
    @dev Priced at the current block: the output amount is the auction's
         quote now, so the intent stays valid while the curve decays below
         it (a standing ask) and a publisher re-posts, with a fresh nonce,
         as the price falls. A limit order (empty context) open to every
         solver, filled and attested by the output settler, input oracle =
         output settler. The fill deadline bounds both the open and the
         fill and defaults to the lot end; a shorter one limits how long an
         opened intent can sit in the escrow (expires = fill deadline, so
         an unfilled intent is refundable right after). Anyone may publish;
         anyone may open it through openFor (paying nothing) and a solver
         settles it atomically through openForAndFinalise.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _nonce Permit2 unordered nonce for the auction; burnt by the
           open, so every published intent needs one no earlier open has
           used.
    @param _sell_amount Amount to sell; 0 sells everything available. The
           intent is fill-or-kill, so it must not exceed what is available.
    @param _fill_deadline Last timestamp (inclusive) the output may be
           filled; 0 means the lot end.
    @return The order and `0x00 ++ this_adapter ++ abi_encode(order)`.
    """
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = staticcall _auction.available(_token)
    assert sell_amount > 0, NothingToSell()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)
    fill_deadline: uint256 = _fill_deadline
    if fill_deadline == 0:
        fill_deadline = lot_end
    assert block.timestamp < fill_deadline and fill_deadline <= lot_end, BadFillDeadline()
    buy_amount: uint256 = staticcall _auction.getAmountNeeded(_token, sell_amount)
    output_settler_id: bytes32 = oif._to_identifier(self.output_settler)

    order: oif.StandardOrder = oif.StandardOrder(
        user=_auction.address,
        nonce=_nonce,
        originChainId=chain.id,
        expires=convert(fill_deadline, uint32),
        fillDeadline=convert(fill_deadline, uint32),
        inputOracle=self.output_settler,
        inputs=[[convert(_token, uint256), sell_amount]],
        outputs=[
            oif.MandateOutput(
                oracle=output_settler_id,
                settler=output_settler_id,
                chainId=chain.id,
                token=oif._to_identifier((staticcall _auction.want()).address),
                amount=buy_amount,
                recipient=oif._to_identifier(staticcall _auction.receiver()),
                callbackData=b"",
                context=b"",
            )
        ],
    )
    return order, concat(SIGNATURE_TYPE_PERMIT2, convert(self, bytes20), abi_encode(order))


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[oif.ENCODED_ORDER_BOUND]) -> bytes4:
    """
    @notice Validate a Permit2 witness digest against the calling auction's
            live lot economics.
    @dev The payload is the bare abi-encoded StandardOrder (the escrow
         stripped its signature-type byte, the router the adapter prefix).
         Protocol checks revert with oif.OrderNotValid(reason); the economic
         check is the auction's check_order, whose typed reverts pass
         through untouched. The calling router lets every revert bubble. A
         payload that does not decode as one order (a second input or
         output, oversized dynamic fields, truncation) fails ABI decoding
         before any check runs and bubbles up the same way; one that
         decodes but is not the order's canonical encoding is NonCanonical.
         Shape: the calling auction as user, this chain as origin, exactly
         one input and one output, clean identifiers, the pinned output
         settler as input oracle and as the output's oracle and settler on
         this chain, no callback, and a context the output settler resolves
         to the signed amount. The fill deadline is the validity bound: the
         escrow opens the intent only before it and the output settler
         fills only up to it. Expires must equal it: the escrow refunds an
         opened but unfilled intent only after expires, so an expires past
         the fill deadline would let any opener park the lot in the escrow
         beyond the lot end.
    @param _hash Permit2 digest the escrow's permitWitnessTransferFrom is
           verifying.
    @param _signature The abi-encoded StandardOrder, prefixes already
           stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: oif.StandardOrder = abi_decode(_signature, oif.StandardOrder)
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the order the escrow itself decodes.
    assert abi_encode(order) == _signature, oif.OrderNotValid(reason="NonCanonical")

    assert oif._permit_digest(order, self.settler, self.domain_separator) == _hash, (
        oif.OrderNotValid(reason="InvalidHash")
    )
    assert order.user == msg.sender, oif.OrderNotValid(reason="BadUser")
    assert order.originChainId == chain.id, oif.OrderNotValid(reason="BadChain")
    assert order.expires == order.fillDeadline, oif.OrderNotValid(reason="BadExpires")
    assert len(order.inputs) == 1, oif.OrderNotValid(reason="BadInputs")
    assert len(order.outputs) == 1, oif.OrderNotValid(reason="BadOutputs")
    assert order.inputOracle == self.output_settler, oif.OrderNotValid(reason="BadInputOracle")

    sell_token_id: uint256 = order.inputs[0][0]
    output: oif.MandateOutput = order.outputs[0]
    output_settler_id: bytes32 = oif._to_identifier(self.output_settler)
    assert output.chainId == chain.id, oif.OrderNotValid(reason="BadOutputChain")
    assert output.settler == output_settler_id and output.oracle == output_settler_id, (
        oif.OrderNotValid(reason="BadOutputSettler")
    )
    assert (
        oif._is_clean(sell_token_id)
        and oif._is_clean(convert(output.token, uint256))
        and oif._is_clean(convert(output.recipient, uint256))
    ), oif.OrderNotValid(reason="BadIdentifier")
    assert len(output.callbackData) == 0, oif.OrderNotValid(reason="BadCallback")
    assert oif._resolves_to_signed_amount(output.context), oif.OrderNotValid(reason="BadContext")

    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure. The
    # native-coin identifier (0) is never a lot (LotInactive) nor the
    # payment token (BadBuyToken).
    assert staticcall IDutchAuction(msg.sender).check_order(
        oif._from_identifier(sell_token_id),
        oif._from_identifier(convert(output.token, uint256)),
        oif._from_identifier(convert(output.recipient, uint256)),
        order.inputs[0][1],
        output.amount,
        convert(order.fillDeadline, uint256),
    )
    return c.ERC1271_MAGIC_VALUE
