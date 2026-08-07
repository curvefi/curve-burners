# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CoW GPv2 execution module
@author Curve Finance
@license MIT
@notice Direct CoW Protocol execution rail: ERC-1271 validation of GPv2
        orders against live auction state, Yearn-auction style.
@dev Self-publishing rail: anyone may POST a canonical GPv2 order for an
     active lot to the CoW orderbook with signing scheme eip1271 and the
     abi-encoded order as the signature — no on-chain registration required,
     validation happens against live lot economics at settlement. The
     ComposableCoW wrapper encoding is accepted for orders published by a
     watchtower and validated through the same economic checks: the wrapper
     is transport, never authority. The importing contract owns
     authorization, custody, router approvals, and lot accounting behind
     compile-time hooks.
"""


from ..auction import adapter_types
from . import gpv2


error BadCowValidity:
    pass


error BadSettlement:
    pass


error BadDomainSeparator:
    pass


error BadVaultRelayer:
    pass


error CowAlreadyEnabled:
    pass


error CowNotEnabled:
    pass


error CowUnconfigured:
    pass


interface Settlement:
    def domainSeparator() -> bytes32: view
    def vaultRelayer() -> address: view


event CowExecutionConfigured:
    settlement: indexed(address)
    vault_relayer: indexed(address)
    domain_separator: bytes32


event CowExecutionEnabled:
    pass


event CowExecutionDisabled:
    pass


# Order parameters fixed at deployment. The importing contract performs any
# calendar-dependent check that cow_order_validity fits its auction frame.
app_data: public(immutable(bytes32))
cow_order_validity: public(immutable(uint256))  # seconds per stable-order bucket

# Direct-rail state. The importing contract deliberately starts unconfigured
# and disabled; the domain separator and relayer are read from the settlement
# at configure time so they can never drift apart.
cow_enabled: public(bool)
settlement: public(address)
vault_relayer: public(address)
cow_domain_separator: public(bytes32)


@deploy
def __init__(_app_data: bytes32, _cow_order_validity: uint256):
    assert _cow_order_validity > 0, BadCowValidity()
    self.app_data = _app_data
    self.cow_order_validity = _cow_order_validity


# Lifecycle (internal: the importing contract keeps authorization visible)


@internal
def _configure_cow(_settlement: address):
    assert not self.cow_enabled, CowAlreadyEnabled()
    assert _settlement != empty(address), BadSettlement()
    domain_separator: bytes32 = staticcall Settlement(_settlement).domainSeparator()
    relayer: address = staticcall Settlement(_settlement).vaultRelayer()
    assert domain_separator != empty(bytes32), BadDomainSeparator()
    assert relayer != empty(address), BadVaultRelayer()

    self.settlement = _settlement
    self.vault_relayer = relayer
    self.cow_domain_separator = domain_separator
    log CowExecutionConfigured(
        settlement=_settlement,
        vault_relayer=relayer,
        domain_separator=domain_separator,
    )


@internal
def _enable_cow():
    assert not self.cow_enabled, CowAlreadyEnabled()
    assert self.settlement != empty(address), CowUnconfigured()
    self.cow_enabled = True
    log CowExecutionEnabled()


@internal
def _disable_cow():
    assert self.cow_enabled, CowNotEnabled()
    self.cow_enabled = False
    log CowExecutionDisabled()


# Direct ERC-1271 validation


@internal
@view
def _validate_cow_signature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4:
    """
    @notice Validate a GPv2 digest against live lot economics.
    @dev Two transport encodings, one authority: a bare abi-encoded GPv2Order
         (self-published, Yearn style) or the ComposableCoW (order, payload)
         wrapper (watchtower-published). The wrapper is not consulted for
         authorization — only the inner order's economics decide, so a
         registration or handler can never weaken settlement checks.
         Reverts with OrderNotValid, keeping the historical embedded-path
         revert semantics; the adapter envelope path answers 0xffffffff.
    """
    if not self.cow_enabled:
        raise gpv2.CowDisabled()
    if not self._cow_signature_allowed():
        gpv2._order_not_valid("Reentrancy")

    order: gpv2.GPv2Order = empty(gpv2.GPv2Order)
    if len(_signature) == gpv2.ENCODED_ORDER_LEN:
        order = abi_decode(_signature, gpv2.GPv2Order)
        if abi_encode(order) != _signature:
            gpv2._order_not_valid("NonCanonical")
    else:
        payload: gpv2.PayloadStruct = empty(gpv2.PayloadStruct)
        order, payload = abi_decode(_signature, (gpv2.GPv2Order, gpv2.PayloadStruct))
        if abi_encode(order, payload) != _signature:
            gpv2._order_not_valid("NonCanonical")

    self._check_cow_order(order, _hash)
    return gpv2.ERC1271_MAGIC_VALUE


@internal
@view
def _check_cow_order(_order: gpv2.GPv2Order, _hash: bytes32):
    """@notice Enforce every economic check on a GPv2 order; revert on failure."""
    if gpv2._order_digest(_order, self.cow_domain_separator) != _hash:
        gpv2._order_not_valid("InvalidHash")
    if _order.appData != self.app_data:
        gpv2._order_not_valid("BadAppData")
    # sellToken != buyToken is implied: buyToken must equal the target and the
    # lot context below rejects the target as a sellable lot token.
    if _order.buyToken != self._cow_target():
        gpv2._order_not_valid("BadToken")
    if _order.receiver != self._cow_receiver():
        gpv2._order_not_valid("BadReceiver")
    if not gpv2._check_order_flags(_order):
        gpv2._order_not_valid("BadOrderFlags")
    if not gpv2._check_balance_modes(_order):
        gpv2._order_not_valid("BadBalanceMode")

    active: bool = False
    available: uint256 = 0
    initial_amount: uint256 = 0
    start: uint256 = 0
    end: uint256 = 0
    active, available, initial_amount, start, end = self._cow_order_context(
        _order.sellToken
    )
    if not active or block.timestamp < start or block.timestamp >= end:
        gpv2._order_not_valid("NotAllowed")
    if available == 0:
        gpv2._order_not_valid("ZeroBalance")
    if _order.sellAmount == 0 or _order.sellAmount > initial_amount:
        gpv2._order_not_valid("BadSellAmount")
    valid_to: uint256 = convert(_order.validTo, uint256)
    if block.timestamp > valid_to or valid_to > end:
        gpv2._order_not_valid("BadValidTo")
    if _order.buyAmount < self._cow_quote(_order.sellToken, _order.sellAmount, block.timestamp):
        gpv2._order_not_valid("BadBuyAmount")


# Compile-time integration hooks implemented by the importing contract.
@internal
@view
@abstract
def _cow_target() -> address: ...


@internal
@view
@abstract
def _cow_receiver() -> address: ...


@internal
@view
@abstract
def _cow_order_context(_token: address) -> (bool, uint256, uint256, uint256, uint256): ...


@internal
@view
@abstract
def _cow_quote(_token: address, _sell_amount: uint256, _timestamp: uint256) -> uint256: ...


@internal
@view
@abstract
def _cow_signature_allowed() -> bool: ...
