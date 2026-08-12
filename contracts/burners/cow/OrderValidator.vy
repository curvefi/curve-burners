# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CowOrderValidator
@author Curve Finance
@license MIT
@notice Standalone CoW Protocol (GPv2) validator adapter for the Dutch auction
        ERC-1271 dispatcher: decodes a direct GPv2 order payload, recomputes the
        canonical EIP-712 settlement digest, and returns the normalized order
        for the auction core's economic checks.
@dev Registry conventions for this adapter: adapter_id is
     keccak("CURVE_COW_GPV2")[:4], authorization_mode is
     MODE_COW_VAULT_RELAYER, verifier is the GPv2 settlement (the EIP-712
     verifying contract) and executor is its vault relayer (the contract that
     pulls the sell token) — both read from the settlement at deploy, so the
     registry config for this adapter must pin the same pair. The registry
     config must also set allow_partial_fills = True: canonical GPv2 orders are
     partially fillable (enforced by the flag check below), so a False config
     silently invalidates every order with 0xffffffff. The payload ABI
     is abi_encode(GPv2Order, auction_epoch): a 13-word static tuple. All
     economic authority stays with the auction core; this contract only proves
     that the supplied digest is the canonical GPv2 hash of the decoded order.
@custom:kill Stateless and immutable: nothing to pause here. Validation through
             this adapter stops immediately when the registry disables the
             adapter_id or any auction disables it locally; both paths leave
             native take() untouched.
@custom:security Holds no funds, receives no allowances, and is reached only
                 through a non-reverting staticcall from the dispatcher, which
                 also pins this contract's codehash via the registry. Every
                 revert here simply becomes an invalid signature. The canonical
                 re-encode check makes the payload bytes a bijection of the
                 returned order, so no two payloads share a digest.
"""

from ..auction import adapter_types
from . import gpv2
from ...interfaces import IOrderValidator

implements: IOrderValidator


error BadSettlement:
    pass


error BadDomainSeparator:
    pass


error BadVaultRelayer:
    pass


error BadPayloadLength:
    pass


error NonCanonicalPayload:
    pass


error BadOrderFlags:
    pass


error BadBalanceModes:
    pass


error DigestMismatch:
    pass


interface GPv2Settlement:
    def domainSeparator() -> bytes32: view
    def vaultRelayer() -> address: view


# Registry conventions, exposed for deployment tooling
ADAPTER_ID: public(constant(bytes4)) = 0xb34cdfce  # keccak("CURVE_COW_GPV2")[:4]
AUTHORIZATION_MODE: public(constant(uint8)) = adapter_types.MODE_COW_VAULT_RELAYER

# abi_encode(GPv2Order, auction_epoch) is a static 13-word tuple.
PAYLOAD_LEN: constant(uint256) = 13 * 32

settlement: public(immutable(GPv2Settlement))
domain_separator: public(immutable(bytes32))
vault_relayer: public(immutable(address))


@deploy
def __init__(_settlement: GPv2Settlement):
    """
    @notice Pin the settlement and its EIP-712 domain at deploy.
    @dev The domain separator is immutable in GPv2Settlement, so reading it once
         is safe; a settlement upgrade requires a fresh adapter version anyway
         because the registry pins this contract's codehash and config.
    @param _settlement Canonical GPv2 settlement contract of this chain.
    """
    assert _settlement.address != empty(address), BadSettlement()
    domain: bytes32 = staticcall _settlement.domainSeparator()
    relayer: address = staticcall _settlement.vaultRelayer()
    assert domain != empty(bytes32), BadDomainSeparator()
    assert relayer != empty(address), BadVaultRelayer()

    self.settlement = _settlement
    self.domain_separator = domain
    self.vault_relayer = relayer


@external
@view
def validate(
    _auction: address,
    _digest: bytes32,
    _payload: Bytes[adapter_types.MAX_ADAPTER_PAYLOAD],
) -> adapter_types.NormalizedOrder:
    """
    @notice Prove that `_digest` is the canonical GPv2 digest of the payload's
            order and normalize it for the auction core.
    @dev Stateless and auction-agnostic: `_auction` is unused because every
         auction-specific check (lot, epoch, price, receiver, context hash)
         belongs to the caller's core. context_hash maps to appData — the
         protocol-hashed field that carries the auction's replay commitment
         inside the signed digest.
    @param _auction Auction being validated for (unused, see dev note).
    @param _digest EIP-712 digest supplied to the dispatcher.
    @param _payload abi_encode(GPv2Order, auction_epoch).
    @return Normalized order with verifier = settlement, executor = vault
            relayer, min_buy_amount = buyAmount, context_hash = appData.
    """
    assert len(_payload) == PAYLOAD_LEN, BadPayloadLength()
    order: gpv2.GPv2Order = empty(gpv2.GPv2Order)
    auction_epoch: uint256 = 0
    order, auction_epoch = abi_decode(_payload, (gpv2.GPv2Order, uint256))
    # Canonical re-encode: the payload must be the unique encoding of the
    # decoded order, so dirty words cannot alias a clean order's digest.
    assert abi_encode(order, auction_epoch) == _payload, NonCanonicalPayload()
    assert gpv2._check_order_flags(order), BadOrderFlags()
    assert gpv2._check_balance_modes(order), BadBalanceModes()

    digest: bytes32 = gpv2._order_digest(order, self.domain_separator)
    assert digest == _digest, DigestMismatch()

    return adapter_types.NormalizedOrder(
        recomputed_digest=digest,
        context_hash=order.appData,
        auction_epoch=auction_epoch,
        sell_token=order.sellToken,
        buy_token=order.buyToken,
        receiver=order.receiver,
        verifier=self.settlement.address,
        executor=self.vault_relayer,
        sell_amount=order.sellAmount,
        min_buy_amount=order.buyAmount,
        valid_to=convert(order.validTo, uint256),
        partially_fillable=order.partiallyFillable,
    )
