# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CowAdapter
@author Curve Finance
@license MIT
@notice Standalone CoW Protocol (GPv2) settlement adapter for Dutch auctions:
        validates ERC-1271 signatures forwarded by an auction's prefix router
        against the canonical GPv2 digest and the auction's shared economic
        order check, and builds publishable orders for keepers.
@dev Registry conventions: adapter = this contract, executor = the vault
     relayer read from the settlement at deploy. Signature template: orders
     are posted with signing scheme eip1271, `from` = the auction, and
     signature `this_adapter ++ abi_encode(GPv2Order)` (see order_for and
     isValidSignature).
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner); native take() stays untouched.
@custom:security Holds no funds and receives no allowances (the executor —
                 the vault relayer — does). Auction-agnostic: every economic
                 decision is delegated to msg.sender's check_order, so calling
                 this contract directly proves nothing about any auction.
"""

from contracts.burners.cow import gpv2
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadSettlement:
    pass


error BadDomainSeparator:
    pass


error BadVaultRelayer:
    pass


error NothingToSell:
    pass


interface Settlement:
    def domainSeparator() -> bytes32: view
    def vaultRelayer() -> address: view


ADAPTER_VERSION: public(constant(String[20])) = "CowAdapter"
# `this_adapter ++ abi_encode(GPv2Order)`: the 20-byte adapter address the
# auction's router strips, then the bare order this contract validates.
SIGNATURE_LEN: constant(uint256) = 20 + gpv2.ENCODED_ORDER_LEN

# GPv2 wiring pinned at deploy: the domain separator is immutable in the
# settlement, so a settlement upgrade means a fresh adapter deployment and a
# new registry entry anyway.
settlement: public(immutable(address))
domain_separator: public(immutable(bytes32))
vault_relayer: public(immutable(address))
# The appData every published order must carry.
app_data: public(immutable(bytes32))


@deploy
def __init__(_settlement: address, _app_data: bytes32):
    """
    @notice Pin the settlement, its EIP-712 domain, and the order appData.
    @param _settlement Canonical GPv2 settlement contract of this chain.
    @param _app_data appData hash every order published for this adapter uses.
    """
    assert _settlement != empty(address), BadSettlement()
    domain: bytes32 = staticcall Settlement(_settlement).domainSeparator()
    relayer: address = staticcall Settlement(_settlement).vaultRelayer()
    assert domain != empty(bytes32), BadDomainSeparator()
    assert relayer != empty(address), BadVaultRelayer()

    self.settlement = _settlement
    self.domain_separator = domain
    self.vault_relayer = relayer
    self.app_data = _app_data


# Publishing


@external
@view
def order_for(
    _auction: IDutchAuction, _token: address, _sell_amount: uint256 = 0
) -> (gpv2.GPv2Order, Bytes[SIGNATURE_LEN]):
    """
    @notice Build the order a publisher posts to the CoW orderbook for a live
            lot, together with its eip1271 signature for the auction.
    @dev Priced at the current block: the buy amount is the auction's quote
         now, so the order stays valid while the curve decays below it (a
         standing ask) and a publisher re-posts as the price falls. Validity
         ends with the lot window. Post with `from` = the auction and the
         returned bytes as the signature; anyone may do so.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _sell_amount Amount to sell; 0 sells everything available.
    @return The GPv2 order and `this_adapter ++ abi_encode(order)`.
    """
    sell_amount: uint256 = _sell_amount
    if sell_amount == 0:
        sell_amount = staticcall _auction.available(_token)
    assert sell_amount > 0, NothingToSell()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = staticcall _auction.window(_token)

    order: gpv2.GPv2Order = gpv2.GPv2Order(
        sellToken=_token,
        buyToken=(staticcall _auction.want()).address,
        receiver=staticcall _auction.receiver(),
        sellAmount=sell_amount,
        buyAmount=staticcall _auction.getAmountNeeded(_token, sell_amount),
        # validTo is inclusive on the CoW side; check_order rejects at lot_end
        # anyway (LotInactive), so nothing settles past the window.
        validTo=convert(lot_end, uint32),
        appData=self.app_data,
        feeAmount=0,
        kind=gpv2.SELL_KIND,
        partiallyFillable=True,
        sellTokenBalance=gpv2.TOKEN_BALANCE,
        buyTokenBalance=gpv2.TOKEN_BALANCE,
    )
    return order, concat(convert(self, bytes20), abi_encode(order))


# Settlement


@external
@view
def isValidSignature(
    _hash: bytes32, _signature: Bytes[gpv2.ENCODED_ORDER_LEN]
) -> bytes4:
    """
    @notice Validate a GPv2 digest against the calling auction's live lot
            economics.
    @dev The payload is the bare abi-encoded GPv2Order (the router already
         stripped the adapter prefix). Protocol checks revert with
         gpv2.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The calling
         router lets every revert bubble. A payload longer than one encoded
         order fails ABI decoding before any check runs and bubbles up the
         same way.
    """
    assert len(_signature) == gpv2.ENCODED_ORDER_LEN, gpv2.OrderNotValid(reason="NonCanonical")
    order: gpv2.GPv2Order = abi_decode(_signature, gpv2.GPv2Order)

    assert gpv2._order_digest(order, self.domain_separator) == _hash, (
        gpv2.OrderNotValid(reason="InvalidHash")
    )
    assert order.appData == self.app_data, gpv2.OrderNotValid(reason="BadAppData")
    assert order.feeAmount == 0 and order.kind == gpv2.SELL_KIND and order.partiallyFillable, gpv2.OrderNotValid(reason="BadOrderFlags")
    assert order.sellTokenBalance == gpv2.TOKEN_BALANCE and order.buyTokenBalance == gpv2.TOKEN_BALANCE, gpv2.OrderNotValid(reason="BadBalanceMode")

    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure.
    assert staticcall IDutchAuction(msg.sender).check_order(
        order.sellToken,
        order.buyToken,
        order.receiver,
        order.sellAmount,
        order.buyAmount,
        convert(order.validTo, uint256),
    )
    return c.ERC1271_MAGIC_VALUE
