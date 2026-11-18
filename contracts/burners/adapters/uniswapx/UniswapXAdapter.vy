# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title UniswapXAdapter
@author Curve Finance
@license MIT
@notice Standalone UniswapX (v1 ExclusiveDutchOrderReactor) settlement
        adapter for Dutch auctions: validates the Permit2 witness digest the
        reactor asks the auction to sign, forwarded by the auction's prefix
        router, against the order it encodes and the auction's shared
        economic order check, and builds publishable orders for fillers.
@dev Registry conventions: adapter = this contract, executor = Permit2 (read
     from the reactor at deploy): the reactor settles an order through
     Permit2.permitWitnessTransferFrom, so Permit2 is what pulls the sold
     tokens and what auctions approve. Signature template: the reactor is
     called with SignedOrder(order = abi.encode(ExclusiveDutchOrder), sig =
     this_adapter ++ abi.encode(ExclusiveDutchOrder)); Permit2 forwards the
     sig verbatim to the swapper's (the auction's) isValidSignature (see
     order_for and isValidSignature). Orders are fill-or-kill: Permit2 burns
     the order nonce on settlement, so each order sells its input once and a
     publisher re-posts a fresh nonce as the curve decays. The reactor's
     linear decay is replicated so the output amount it resolves now is what
     is priced; exclusivity overrides and protocol fees only add to what the
     filler pays, never subtract.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner); native take() stays untouched.
@custom:security Holds no funds and receives no allowances (the executor —
                 Permit2 — does, and spends them only under a digest this
                 adapter validated through the auction's router).
                 Auction-agnostic: every economic decision is delegated to
                 msg.sender's check_order, so calling this contract directly
                 proves nothing about any auction. Validity here means the
                 auction accepts the fill; reactor-side liveness (an unused
                 nonce, exclusivity, the validation hook) is the publisher's
                 concern and cannot be read from a signature check.
"""

from contracts.burners.adapters.uniswapx import uniswapx
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadReactor:
    pass


error BadPermit2:
    pass


error BadDomainSeparator:
    pass


error NothingToSell:
    pass


error ExceedsAvailable:
    pass


interface Reactor:
    def permit2() -> address: view


interface Permit2:
    def DOMAIN_SEPARATOR() -> bytes32: view


ADAPTER_VERSION: public(constant(String[20])) = "UniswapXAdapter"
# `this_adapter ++ abi_encode(ExclusiveDutchOrder)`: the 20-byte adapter
# address the auction's router strips, then the bare order this contract
# validates.
SIGNATURE_LEN: constant(uint256) = 20 + uniswapx.ENCODED_ORDER_BOUND

# UniswapX wiring pinned at deploy: the reactor decides the decay semantics
# and is the permit spender, Permit2 is the signer's verifying contract. A
# reactor upgrade means a fresh adapter deployment and a new registry entry.
reactor: public(immutable(address))
permit2: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_reactor: address):
    """
    @notice Pin the reactor, its Permit2, and Permit2's EIP-712 domain.
    @param _reactor ExclusiveDutchOrderReactor of this chain.
    """
    assert _reactor != empty(address), BadReactor()
    permit_2: address = staticcall Reactor(_reactor).permit2()
    assert permit_2 != empty(address), BadPermit2()
    domain: bytes32 = staticcall Permit2(permit_2).DOMAIN_SEPARATOR()
    assert domain != empty(bytes32), BadDomainSeparator()

    self.reactor = _reactor
    self.permit2 = permit_2
    self.domain_separator = domain


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _nonce: uint256, _sell_amount: uint256 = 0
) -> (uniswapx.ExclusiveDutchOrder, Bytes[SIGNATURE_LEN]):
    """
    @notice Build the order a publisher hands to UniswapX fillers for a live
            lot, together with its signature for the auction.
    @dev Priced at the current block as a fixed-price order (no reactor-side
         decay): the output is the auction's quote now, so the order stays
         valid while the curve decays below it (a standing ask) and a
         publisher re-posts, with a fresh nonce, as the price falls. The
         reactor's linear decay cannot track the stepped curve, so it is not
         used. Validity ends with the lot window. Open to every filler (no
         exclusivity, no validation hook). Anyone may publish.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _nonce Permit2 unordered nonce for the auction; burnt by the fill,
           so every published order needs one no earlier fill has used.
    @param _sell_amount Amount to sell; 0 sells everything available. The
           order is fill-or-kill, so it must not exceed what is available.
    @return The order and `this_adapter ++ abi_encode(order)`.
    """
    available: uint256 = staticcall _auction.available(_token)
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = available
    assert sell_amount > 0, NothingToSell()
    assert sell_amount <= available, ExceedsAvailable()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)
    buy_amount: uint256 = staticcall _auction.getAmountNeeded(_token, sell_amount)

    order: uniswapx.ExclusiveDutchOrder = uniswapx.ExclusiveDutchOrder(
        info=uniswapx.OrderInfo(
            reactor=self.reactor,
            swapper=_auction.address,
            nonce=_nonce,
            # The deadline is inclusive on both the reactor and Permit2;
            # check_order rejects at lot_end anyway (LotInactive), so
            # nothing settles past the window.
            deadline=lot_end,
            additionalValidationContract=empty(address),
            additionalValidationData=b"",
        ),
        decayStartTime=block.timestamp,
        decayEndTime=block.timestamp,
        exclusiveFiller=empty(address),
        exclusivityOverrideBps=0,
        input=uniswapx.DutchInput(token=_token, startAmount=sell_amount, endAmount=sell_amount),
        outputs=[
            uniswapx.DutchOutput(
                token=(staticcall _auction.want()).address,
                startAmount=buy_amount,
                endAmount=buy_amount,
                recipient=staticcall _auction.receiver(),
            )
        ],
    )
    return order, concat(convert(self, bytes20), abi_encode(order))


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[uniswapx.ENCODED_ORDER_BOUND]) -> bytes4:
    """
    @notice Validate a Permit2 witness digest against the calling auction's
            live lot economics.
    @dev The payload is the bare abi-encoded ExclusiveDutchOrder (the router
         already stripped the adapter prefix). Protocol checks revert with
         uniswapx.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The calling
         router lets every revert bubble. A payload that does not decode as
         one order (a second output, oversized validation data, truncation)
         fails ABI decoding before any check runs and bubbles up the same
         way; one that decodes but is not the order's canonical encoding is
         NonCanonical. The input must not decay (the lot sells a fixed amount);
         the single output is priced at the amount the reactor resolves now,
         a floor on what any filler pays. The fill is atomic, so the input
         must also be available right now, not only within the lot snapshot.
    @param _hash Permit2 digest the reactor's permitWitnessTransferFrom is
           verifying.
    @param _signature The abi-encoded ExclusiveDutchOrder, prefix already
           stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    order: uniswapx.ExclusiveDutchOrder = abi_decode(_signature, uniswapx.ExclusiveDutchOrder)
    # The decoder tolerates trailing bytes and loose offsets; the payload
    # must be the one encoding of the order the reactor itself decodes.
    assert abi_encode(order) == _signature, uniswapx.OrderNotValid(reason="NonCanonical")

    assert uniswapx._permit_digest(order, self.domain_separator) == _hash, (
        uniswapx.OrderNotValid(reason="InvalidHash")
    )
    assert order.info.reactor == self.reactor, uniswapx.OrderNotValid(reason="BadReactor")
    assert order.info.swapper == msg.sender, uniswapx.OrderNotValid(reason="BadSwapper")
    assert order.input.startAmount == order.input.endAmount, (
        uniswapx.OrderNotValid(reason="InputDecay")
    )
    assert len(order.outputs) == 1, uniswapx.OrderNotValid(reason="BadOutputs")
    output: uniswapx.DutchOutput = order.outputs[0]
    min_buy_amount: uint256 = uniswapx._decayed_output(
        output, order.decayStartTime, order.decayEndTime
    )

    auction: IDutchAuction = IDutchAuction(msg.sender)
    assert order.input.startAmount <= staticcall auction.available(order.input.token), (
        uniswapx.OrderNotValid(reason="ExceedsAvailable")
    )
    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure.
    assert staticcall auction.check_order(
        order.input.token,
        output.token,
        output.recipient,
        order.input.startAmount,
        min_buy_amount,
        order.info.deadline,
    )
    return c.ERC1271_MAGIC_VALUE
