# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title ZeroExSettlerAdapter
@author Curve Finance
@license MIT
@notice Standalone 0x Settler v2 gasless settlement adapter for Dutch
        auctions (SettlerMetaTxn, the Gasless API rail, or SettlerIntent,
        the solver rail): validates the Permit2 witness digest the Settler
        asks the auction (the taker) to sign, forwarded by the auction's
        prefix router, against the order it encodes and the auction's shared
        economic order check, and builds publishable orders.
@dev Registry conventions: adapter = this contract, executor = Permit2: the
     Settler pulls the sold token through Permit2.permitWitnessTransferFrom
     (its first action is a METATXN_*_VIP action carrying the permit), so
     Permit2 is what pulls the sold tokens and what auctions approve.
     Signature template: executeMetaTxn(slippage, actions, zid, msgSender =
     auction, sig = this_adapter ++ abi.encode(Order)); the Settler hands
     sig to Permit2, which forwards it verbatim to the taker's (the
     auction's) isValidSignature (see order_for and isValidSignature). One
     deployment pins one Settler contract and its flavor: the Settler is the
     signed permit spender and the flavor decides the witness type
     (SettlerMetaTxn signs SlippageAndActions, SettlerIntent signs Slippage;
     a flavor mismatch fails every digest, InvalidHash), and the 0x
     deployer/registry it must stay listed in: 0x retires a Settler only
     there (a newer deployment, or remove for a faulty one), the retired
     bytecode keeps settling, and 0x's integration guidance is to query
     ownerOf(feature) / prev(feature) before signing rather than hardcode
     a Settler. So the deploy probes the registry (the Settler is a 0x
     deployment of the flavor's feature, 3 for SettlerMetaTxn or 4 for
     SettlerIntent, and the feature's current or previous instance) and
     every validation repeats the liveness part: the adapter stops signing
     once 0x has removed its Settler or deployed two newer ones, as that
     guidance intends; a new adapter deployment follows the registry. A
     Settler upgrade thus means a fresh adapter deployment and a new
     registry entry on our side.
     Settlement is atomic: the pull, the swap actions and the slippage check
     run in one executeMetaTxn and nothing is escrowed, so the only time
     bound is the permit deadline (at most the lot end) and no fill window
     is needed. Orders are fill-or-kill: Permit2 burns the nonce on
     settlement and the Settler pulls exactly the permitted amount.
@custom:kill Stateless and immutable: nothing to pause here. Routing through
             this adapter stops immediately when the registry disables it
             (owner or emergency owner), and by itself once the 0x
             deployer/registry no longer lists the pinned Settler as its
             feature's current or previous instance (0x's pause, reported
             as OrderNotValid(SettlerRetired)); native take() stays
             untouched.
@custom:security Holds no funds and receives no allowances (the executor —
                 Permit2 — does, and spends them only under a digest this
                 adapter validated through the auction's router).
                 Auction-agnostic: every economic decision is delegated to
                 msg.sender's check_order, so calling this contract directly
                 proves nothing about any auction. The floor is the signed
                 slippage: after every action SettlerBase
                 _checkSlippageAndTransfer requires at least minAmountOut of
                 buyToken on the Settler and pays it to recipient, and no
                 action reads or rewrites the slippage, so the signed
                 actions (SettlerMetaTxn) or the solver's (SettlerIntent)
                 only decide the margin above it: fee actions and routing
                 come out of the permitted amount and the surplus. The
                 actions are therefore not carried in the payload, only
                 their Settler hash, which the digest binds. Validity here
                 means the auction accepts the fill; Settler-side liveness
                 (an unused nonce, a relayer, an allow-listed solver for
                 SettlerIntent) is the publisher's concern and cannot be
                 read from a signature check. Permit2 lets a revert of the
                 signature check bubble unchanged (a typed reason reaches
                 the relayer) and reports a non-magic answer, such as the
                 router's 0xffffffff for a disabled adapter, as its own
                 InvalidContractSignature. The 0x deployer/registry is
                 trusted as 0x's pause switch only: it is upgradeable (UUPS,
                 0x governance) and the floor rests on the pinned Settler's
                 code, so a registry that keeps listing a faulty Settler is
                 0x's call, as for every 0x integrator, and the auction
                 registry's disable stays the local kill.
"""

from contracts.burners.adapters.zerox import settler as zerox
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadSettler:
    pass


error BadDeployer:
    pass


error BadFlavor:
    pass


error SettlerRetired:
    pass


error BadPermit2:
    pass


error BadDomainSeparator:
    pass


error NothingToSell:
    pass


error ExceedsAvailable:
    pass


error BadDeadline:
    pass


error BadActionsHash:
    pass


interface Permit2:
    def DOMAIN_SEPARATOR() -> bytes32: view


# The 0x Settler deployer/registry (0x00000000000004533Fe15556B1E086BB1A72cEae
# on every chain): an ERC-721 whose token per feature is owned by the
# feature's current Settler. ownerOf reverts (ERC721NonexistentToken) once a
# feature has no instance left, prev (NoInstance) when the current one has
# no predecessor, deployInfo (ERC721InvalidOwner) for an address it never
# deployed.
interface ZeroExDeployer:
    def ownerOf(tokenId: uint256) -> address: view
    def prev(feature: uint128) -> address: view
    def deployInfo(instance: address) -> (uint128, uint32): view


ADAPTER_VERSION: public(constant(String[20])) = "ZeroExSettlerAdapter"
# `this_adapter ++ abi_encode(Order)`: the 20-byte adapter address the
# auction's router strips, then the bare order this contract validates.
SIGNATURE_LEN: constant(uint256) = 20 + zerox.ENCODED_ORDER_LEN

# Registry features (ERC-721 token ids) of the two gasless Settler flavors
# (SettlerMetaTxn._tokenId, SettlerIntent._tokenId).
FEATURE_METATXN: constant(uint256) = 3
FEATURE_INTENT: constant(uint256) = 4

# 0x wiring pinned at deploy: the Settler is the permit spender and its
# flavor picks the witness type and the registry feature it must stay listed
# under; Permit2 is the signer's verifying contract.
settler: public(immutable(address))
intent: public(immutable(bool))
deployer: public(immutable(address))
feature: public(immutable(uint256))
permit2: public(immutable(address))
domain_separator: public(immutable(bytes32))


@deploy
def __init__(_settler: address, _deployer: address, _permit2: address, _intent: bool):
    """
    @notice Pin the Settler, its flavor and the 0x deployer/registry it is
            listed in, and Permit2 with its EIP-712 domain.
    @dev The registry must know the Settler under the flavor's feature
         (deployInfo: a 0x deployment of the right flavor, BadFlavor
         otherwise; deployInfo itself reverts for an address 0x never
         deployed) and still list it as the feature's current or previous
         instance (SettlerRetired otherwise), the check every validation
         repeats.
    @param _settler The Settler contract (one SettlerMetaTxn or SettlerIntent
           deployment; the live ones are the deployer registry's
           ownerOf(3) and ownerOf(4), their predecessors prev(3) and
           prev(4) keep settling too).
    @param _deployer The 0x Settler deployer/registry.
    @param _permit2 The Permit2 the Settler pulls through.
    @param _intent True for a SettlerIntent (Slippage witness, solver
           submitted), False for a SettlerMetaTxn (SlippageAndActions
           witness, permissionless relay).
    """
    assert _settler.is_contract, BadSettler()
    assert _deployer != empty(address), BadDeployer()
    assert _permit2 != empty(address), BadPermit2()
    domain: bytes32 = staticcall Permit2(_permit2).DOMAIN_SEPARATOR()
    assert domain != empty(bytes32), BadDomainSeparator()

    expected_feature: uint256 = FEATURE_METATXN
    if _intent:
        expected_feature = FEATURE_INTENT
    registry: ZeroExDeployer = ZeroExDeployer(_deployer)
    deployed_feature: uint128 = 0
    deployed_nonce: uint32 = 0
    deployed_feature, deployed_nonce = staticcall registry.deployInfo(_settler)
    assert convert(deployed_feature, uint256) == expected_feature, BadFlavor()
    assert self._listed(registry, expected_feature, _settler), SettlerRetired()

    self.settler = _settler
    self.intent = _intent
    self.deployer = _deployer
    self.feature = expected_feature
    self.permit2 = _permit2
    self.domain_separator = domain


@internal
@view
def _listed(_registry: ZeroExDeployer, _feature: uint256, _settler: address) -> bool:
    """
    @notice Whether the registry still lists the Settler for its feature:
            0x's requireGenuineSettler, the current instance (ownerOf) or
            the previous one (prev, which the 0x API keeps serving during
            the dwell time after a deployment).
    @dev One registry staticcall while the Settler is current, two once it
         is the previous one. A registry revert bubbles and fails closed:
         ownerOf reverts once 0x has removed every instance of the feature,
         prev when the current instance has none before it.
    """
    if staticcall _registry.ownerOf(_feature) == _settler:
        return True
    return staticcall _registry.prev(convert(_feature, uint128)) == _settler


# Publishing


@external
@pure
def hash_actions(
    _actions: DynArray[Bytes[zerox.MAX_ACTION_LEN], zerox.MAX_ACTIONS],
) -> bytes32:
    """
    @notice The Settler's hash of an action list (SettlerMetaTxn
            _hashArrayOfBytes), the actions_hash a SettlerMetaTxn order
            carries. For eth_call use by publishers.
    @param _actions The actions exactly as passed to executeMetaTxn.
    @return keccak of the packed keccaks of every action.
    """
    return zerox._hash_actions(_actions)


@external
@view
def order_for(
    _auction: IDutchAuction,
    _token: address,
    _nonce: uint256,
    _sell_amount: uint256 = 0,
    _deadline: uint256 = 0,
    _actions_hash: bytes32 = empty(bytes32),
) -> (zerox.Order, Bytes[SIGNATURE_LEN]):
    """
    @notice Build the order a publisher hands to the Settler's relayers or
            solvers for a live lot, together with its signature for the
            auction.
    @dev Priced at the current block: minAmountOut is the auction's quote
         now, so the order stays valid while the curve decays below it (a
         standing ask) and a publisher re-posts, with a fresh nonce, as the
         price falls. Validity ends with the deadline, at most the lot end.
         For a SettlerMetaTxn the action list is authored off-chain (the
         Gasless API quote, or a route of the publisher's own) and only its
         hash is carried; for a SettlerIntent the solver routes and no
         actions are signed. Anyone may publish; relaying a SettlerMetaTxn
         order is permissionless, a SettlerIntent order needs one of the
         Settler's solvers.
    @param _auction Auction (DutchAuctionBurner) selling the lot.
    @param _token Lot token to sell.
    @param _nonce Permit2 unordered nonce for the auction; burnt by the fill,
           so every published order needs one no earlier fill has used.
    @param _sell_amount Amount to sell; 0 sells everything available. The
           order is fill-or-kill, so it must not exceed what is available.
    @param _deadline Last timestamp (inclusive) the permit is valid; 0 means
           the lot end.
    @param _actions_hash hash_actions of the signed action list
           (SettlerMetaTxn, required); must be empty for a SettlerIntent.
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
    deadline: uint256 = _deadline
    if deadline == 0:
        deadline = lot_end
    assert block.timestamp < deadline and deadline <= lot_end, BadDeadline()
    assert (_actions_hash == empty(bytes32)) == self.intent, BadActionsHash()

    order: zerox.Order = zerox.Order(
        permit=zerox.Permit(token=_token, amount=sell_amount, nonce=_nonce, deadline=deadline),
        slippage=zerox.Slippage(
            recipient=staticcall _auction.receiver(),
            buyToken=(staticcall _auction.want()).address,
            minAmountOut=staticcall _auction.getAmountNeeded(_token, sell_amount),
        ),
        actions_hash=_actions_hash,
    )
    return order, concat(convert(self, bytes20), abi_encode(order))


@external
@view
def describe(_payload: Bytes[zerox.ENCODED_ORDER_LEN]) -> (zerox.Order, bytes32):
    """
    @notice Decode a bare payload and compute the Permit2 digest this
            adapter would accept it for, so a publisher can compare it with
            the EIP-712 hash a quote asks the auction to sign.
    @param _payload abi_encode(Order), the signature without the adapter
           prefix.
    @return The order and its PermitWitnessTransferFrom digest under the
            pinned Settler, flavor and Permit2 domain.
    """
    order: zerox.Order = abi_decode(_payload, zerox.Order)
    return order, zerox._permit_digest(order, self.settler, self.domain_separator, self.intent)


# Settlement


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[zerox.ENCODED_ORDER_LEN]) -> bytes4:
    """
    @notice Validate a Permit2 witness digest against the calling auction's
            live lot economics.
    @dev The payload is the bare abi-encoded Order (the router already
         stripped the adapter prefix). Protocol checks revert with
         zerox.OrderNotValid(reason); the economic check is the auction's
         check_order, whose typed reverts pass through untouched. The
         calling router lets every revert bubble. Between the two, the
         pinned Settler must still be the one the 0x deployer/registry
         lists for its feature, as its current or previous instance
         (SettlerRetired otherwise: 0x removed it, or deployed two newer
         ones); that is 0x's pause switch and costs one or two registry
         staticcalls per validation. The order is static, so
         its only encoding is exactly ENCODED_ORDER_LEN bytes (NonCanonical
         otherwise; a longer payload fails ABI decoding before any check
         runs). The digest binds the permit, the pinned Settler as spender
         and the flavor's witness (the slippage, plus the action hash for a
         SettlerMetaTxn), so a payload whose permit or slippage differs from
         what the Settler passes Permit2 is InvalidHash. A SettlerIntent
         order carries no action hash (BadActionsHash otherwise): its
         actions are the solver's and unsigned. The fill is atomic and pulls
         exactly the permitted amount, so it must be available right now,
         not only within the lot snapshot. A native buy token (the Settler's
         ETH sentinel) is never the auction's want and fails check_order.
    @param _hash Permit2 digest the Settler's permitWitnessTransferFrom is
           verifying.
    @param _signature The abi-encoded Order, prefix already stripped.
    @return ERC-1271 magic value for a fillable order (anything else reverts).
    """
    assert len(_signature) == zerox.ENCODED_ORDER_LEN, zerox.OrderNotValid(reason="NonCanonical")
    order: zerox.Order = abi_decode(_signature, zerox.Order)

    assert (
        zerox._permit_digest(order, self.settler, self.domain_separator, self.intent) == _hash
    ), zerox.OrderNotValid(reason="InvalidHash")
    if self.intent:
        assert order.actions_hash == empty(bytes32), zerox.OrderNotValid(reason="BadActionsHash")
    # 0x's pause: a Settler the registry no longer lists keeps executing,
    # so the adapter stops signing for it on its own.
    assert self._listed(ZeroExDeployer(self.deployer), self.feature, self.settler), (
        zerox.OrderNotValid(reason="SettlerRetired")
    )

    auction: IDutchAuction = IDutchAuction(msg.sender)
    assert order.permit.amount <= staticcall auction.available(order.permit.token), (
        zerox.OrderNotValid(reason="ExceedsAvailable")
    )
    # The shared economic check: the calling auction prices the fill against
    # its live curve and reverts with its own typed error on failure.
    assert staticcall auction.check_order(
        order.permit.token,
        order.slippage.buyToken,
        order.slippage.recipient,
        order.permit.amount,
        order.slippage.minAmountOut,
        order.permit.deadline,
    )
    return c.ERC1271_MAGIC_VALUE
