# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CowAdapter
@author Curve Finance
@license MIT
@notice Standalone CoW Protocol (GPv2) settlement adapter for Dutch auctions:
        validates ERC-1271 signatures forwarded by an auction's signature
        router against the canonical GPv2 digest and the auction's shared
        economic order check.
@dev Serves as the auction's fallback adapter: the two historical CoW
     signature encodings carry no verifier prefix. Both are accepted — a bare
     abi-encoded GPv2Order (self-published, Yearn style) and the ComposableCoW
     (order, payload) wrapper (watchtower-published); the wrapper is
     transport, never authority. This contract proves only that the digest is
     the canonical GPv2 hash of the decoded order and that the protocol
     constants match; all pricing runs through the calling auction's
     check_order view (msg.sender is the auction), whose reasons are reverted
     verbatim in the watchtower-canonical OrderNotValid ABI. Registry
     conventions: verifier = this contract, executor = the vault relayer read
     from the settlement at deploy.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it or
             an auction disables it locally; both paths leave native take()
             untouched.
@custom:security Holds no funds and receives no allowances (the executor —
                 the vault relayer — does). Reached only through the
                 auction's staticcall router, and auction-agnostic: every
                 economic decision is delegated to msg.sender's check_order.
"""

from contracts.burners.auction.adapters import adapter_types
from contracts.burners.cow import gpv2


error BadSettlement:
    pass


error BadDomainSeparator:
    pass


error BadVaultRelayer:
    pass


error ZeroOrderValidity:
    pass


interface DutchAuction:
    def check_order(
        _sell_token: address,
        _buy_token: address,
        _receiver: address,
        _sell_amount: uint256,
        _min_buy_amount: uint256,
        _valid_to: uint256,
    ) -> String[32]: view


interface Settlement:
    def domainSeparator() -> bytes32: view
    def vaultRelayer() -> address: view


ADAPTER_VERSION: public(constant(String[20])) = "CowAdapter"

# GPv2 wiring pinned at deploy: the domain separator is immutable in the
# settlement, so a settlement upgrade means a fresh adapter deployment and a
# new registry entry anyway.
settlement: public(immutable(address))
domain_separator: public(immutable(bytes32))
vault_relayer: public(immutable(address))
# Order parameters shared with the watchtower handler: the appData every
# order must carry and the stable-order bucket length used for quoting.
app_data: public(immutable(bytes32))
order_validity: public(immutable(uint256))


@deploy
def __init__(_settlement: address, _app_data: bytes32, _order_validity: uint256):
    """
    @notice Pin the settlement, its EIP-712 domain, and the order parameters.
    @param _settlement Canonical GPv2 settlement contract of this chain.
    @param _app_data appData hash every order published for this adapter uses.
    @param _order_validity Seconds per stable-order bucket (handler quoting).
    """
    assert _settlement != empty(address), BadSettlement()
    assert _order_validity > 0, ZeroOrderValidity()
    domain: bytes32 = staticcall Settlement(_settlement).domainSeparator()
    relayer: address = staticcall Settlement(_settlement).vaultRelayer()
    assert domain != empty(bytes32), BadDomainSeparator()
    assert relayer != empty(address), BadVaultRelayer()

    self.settlement = _settlement
    self.domain_separator = domain
    self.vault_relayer = relayer
    self.app_data = _app_data
    self.order_validity = _order_validity


@external
@view
def isValidSignature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_SIGNATURE_LEN]
) -> bytes4:
    """
    @notice Validate a GPv2 digest against the calling auction's live lot
            economics.
    @dev Two transport encodings, one authority: a bare abi-encoded GPv2Order
         or the ComposableCoW (order, payload) wrapper. The wrapper is not
         consulted for authorization — only the inner order's fields reach
         check_order, so a registration or handler can never weaken
         settlement checks. Reverts with the watchtower-canonical
         OrderNotValid(reason); the calling router lets the revert bubble.
    """
    order: gpv2.GPv2Order = empty(gpv2.GPv2Order)
    if len(_signature) == gpv2.ENCODED_ORDER_LEN:
        order = abi_decode(_signature, gpv2.GPv2Order)
        if abi_encode(order) != _signature:
            raise gpv2.OrderNotValid(reason="NonCanonical")
    else:
        payload: gpv2.PayloadStruct = empty(gpv2.PayloadStruct)
        order, payload = abi_decode(_signature, (gpv2.GPv2Order, gpv2.PayloadStruct))
        if abi_encode(order, payload) != _signature:
            raise gpv2.OrderNotValid(reason="NonCanonical")

    if gpv2._order_digest(order, self.domain_separator) != _hash:
        raise gpv2.OrderNotValid(reason="InvalidHash")
    if order.appData != self.app_data:
        raise gpv2.OrderNotValid(reason="BadAppData")
    if not gpv2._check_order_flags(order):
        raise gpv2.OrderNotValid(reason="BadOrderFlags")
    if not gpv2._check_balance_modes(order):
        raise gpv2.OrderNotValid(reason="BadBalanceMode")

    # The shared economic check: the calling auction prices the fill against
    # its live curve and answers with a canonical reason on failure.
    reason: String[32] = staticcall DutchAuction(msg.sender).check_order(
        order.sellToken,
        order.buyToken,
        order.receiver,
        order.sellAmount,
        order.buyAmount,
        convert(order.validTo, uint256),
    )
    if len(reason) != 0:
        raise gpv2.OrderNotValid(reason=reason)
    return gpv2.ERC1271_MAGIC_VALUE
