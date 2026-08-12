# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Auction adapter layer
@author Curve Finance
@license MIT
@notice External-settlement plumbing around the auction core: refcounted
        canonical router approvals, the registry adapter set, and the
        versioned ERC-1271 envelope dispatcher.
@dev The importing contract wires this layer to the auction through
     compile-time hooks: authorization (_owner/_emergency_owner), router
     discovery (_cow_router), the protected payment token (_auction_want),
     the embedded-signature rail (_validate_embedded_signature), and the
     economic order check (_check_order_against_lot). The adapter registry is
     a trusted immutable deployment dependency: its get_adapter view is called
     with a plain staticcall, while untrusted validator adapters are isolated
     behind a non-reverting raw staticcall. The registry is authoritative for
     adapter configs: this layer only caches the resolved router, validation
     rejects signatures whenever that cache disagrees with the live config,
     and refresh_adapter_router permissionlessly realigns it after a registry
     version update. Error asymmetry is deliberate: the embedded (no-magic)
     signature path keeps ComposableCoW's revert semantics, the adapter
     (magic) path answers 0xffffffff without reverting.
"""


from . import adapter_types


error OnlyOwner:
    pass


error ApproveResetFailed:
    pass


error ApproveFailed:
    pass


error BadRouter:
    pass


error CowRouterUnset:
    pass


error BadAuthorizationMode:
    pass


error Permit2Unset:
    pass


error NoRegistry:
    pass


error AlreadyEnabled:
    pass


error NotEnabled:
    pass


error UnknownAdapter:
    pass


error InactiveAdapter:
    pass


interface ERC20:
    def approve(_spender: address, _amount: uint256) -> bool: nonpayable
    def allowance(_owner: address, _spender: address) -> uint256: view


interface AdapterRegistry:
    def get_adapter(_adapter_id: bytes4) -> adapter_types.AdapterConfig: view


event AdapterEnabled:
    adapter_id: indexed(bytes4)
    version: uint16
    router: address


event AdapterDisabled:
    adapter_id: indexed(bytes4)
    router: address


event AdapterRouterRefreshed:
    adapter_id: indexed(bytes4)
    version: uint16
    old_router: address
    new_router: address


event RouterApproval:
    token: indexed(ERC20)
    router: indexed(address)
    amount: uint256


MAX_COINS: constant(uint256) = 64

ERC1271_MAGIC_VALUE: constant(bytes4) = 0x1626ba7e
INVALID_SIGNATURE: constant(bytes4) = 0xffffffff
# abi_encode(NormalizedOrder) is a static 12-word tuple.
NORMALIZED_ORDER_LEN: constant(uint256) = 12 * 32
ADDRESS_BOUND: constant(uint256) = 2**160

# Adapter infrastructure, fixed at deployment (either may be unset)
registry: public(immutable(AdapterRegistry))
permit2: public(immutable(address))

# Adapter set and canonical router approvals. adapter_router caches the router
# resolved from the registry config so disable releases the same refcount it
# retained. The registry stays authoritative: validation rejects signatures
# while the cache disagrees with the live config, and the permissionless
# refresh_adapter_router realigns the cache and both refcounts.
enabled_adapters: public(HashMap[bytes4, bool])
adapter_router: public(HashMap[bytes4, address])
router_refcount: public(HashMap[address, uint256])


@deploy
def __init__(_registry: address, _permit2: address):
    self.registry = AdapterRegistry(_registry)
    self.permit2 = _permit2


# Canonical router approvals


@internal
def _set_infinite_allowance(_token: ERC20, _router: address):
    if staticcall _token.allowance(self, _router) == max_value(uint256):
        return
    # Approve-zero-first supports USDT-style tokens that reject nonzero-to-nonzero.
    assert extcall _token.approve(_router, 0, default_return_value=True), ApproveResetFailed()
    assert extcall _token.approve(
        _router, max_value(uint256), default_return_value=True
    ), ApproveFailed()
    log RouterApproval(token=_token, router=_router, amount=max_value(uint256))


@internal
def _clear_allowance(_token: ERC20, _router: address):
    if staticcall _token.allowance(self, _router) == 0:
        return
    assert extcall _token.approve(_router, 0, default_return_value=True), ApproveResetFailed()
    log RouterApproval(token=_token, router=_router, amount=0)


@internal
def _try_approve(_token: ERC20, _router: address, _amount: uint256) -> bool:
    # Non-reverting approve for the best-effort staging path: raw_call is
    # justified here because a token rejecting approvals must not revert
    # staging. Missing return data counts as success (legacy ERC20).
    success: bool = False
    response: Bytes[32] = b""
    success, response = raw_call(
        _token.address,
        abi_encode(_router, _amount, method_id=method_id("approve(address,uint256)")),
        max_outsize=32,
        revert_on_failure=False,
    )
    if not success:
        return False
    return len(response) == 0 or convert(response, uint256) != 0


@internal
def _try_set_infinite_allowance(_token: ERC20, _router: address):
    if staticcall _token.allowance(self, _router) == max_value(uint256):
        return
    # Approve-zero-first supports USDT-style tokens that reject nonzero-to-nonzero.
    if not self._try_approve(_token, _router, 0):
        return
    if self._try_approve(_token, _router, max_value(uint256)):
        log RouterApproval(token=_token, router=_router, amount=max_value(uint256))


@internal
def _ensure_router_approvals(_token: ERC20):
    # The approval surface is a closed enumerable set of canonical routers;
    # a mutable registry can never add a spender to it. Best-effort: a token
    # whose approve fails must not block staging — its native take stays
    # available and sync_router_approvals is the strict, loud retry path.
    cow_router: address = self._cow_router()
    if cow_router != empty(address) and self.router_refcount[cow_router] > 0:
        self._try_set_infinite_allowance(_token, cow_router)
    if (
        self.permit2 != empty(address)
        and self.permit2 != cow_router
        and self.router_refcount[self.permit2] > 0
    ):
        self._try_set_infinite_allowance(_token, self.permit2)


@external
def sync_router_approvals(_router: address, _tokens: DynArray[ERC20, MAX_COINS]):
    """
    @notice Permissionlessly drive token allowances to the router's target state.
    @dev The target is derived, never caller-chosen: infinity while the router
         is referenced by an enabled rail, zero once fully released. Serves as
         the retry path after approval failures and as retired-router cleanup.
    @param _router Router whose allowances are synchronized.
    @param _tokens Tokens to synchronize.
    """
    assert _router != empty(address), BadRouter()
    grant: bool = self.router_refcount[_router] > 0
    for token: ERC20 in _tokens:
        assert token.address != self._auction_want(), adapter_types.TargetToken()
        if grant:
            self._set_infinite_allowance(token, _router)
        else:
            self._clear_allowance(token, _router)


# Adapter set


@internal
@view
def _resolve_router_lax(_mode: uint8) -> (bool, address):
    """
    @notice Non-reverting router resolution for the validation view path.
    @dev MODE_NONE resolves to the empty router; any mode whose router is
         unset (or an unknown mode) answers not-ok instead of reverting.
    """
    if _mode == adapter_types.MODE_NONE:
        return True, empty(address)
    if _mode == adapter_types.MODE_COW_VAULT_RELAYER:
        router: address = self._cow_router()
        return router != empty(address), router
    if _mode in [
        adapter_types.MODE_PERMIT2_SIGNATURE_TRANSFER,
        adapter_types.MODE_PERMIT2_ALLOWANCE_TRANSFER,
    ]:
        return self.permit2 != empty(address), self.permit2
    return False, empty(address)


@internal
@view
def _resolve_router(_mode: uint8) -> address:
    if _mode == adapter_types.MODE_NONE:
        return empty(address)
    if _mode == adapter_types.MODE_COW_VAULT_RELAYER:
        router: address = self._cow_router()
        assert router != empty(address), CowRouterUnset()
        return router
    assert _mode in [
        adapter_types.MODE_PERMIT2_SIGNATURE_TRANSFER,
        adapter_types.MODE_PERMIT2_ALLOWANCE_TRANSFER,
    ], BadAuthorizationMode()
    assert self.permit2 != empty(address), Permit2Unset()
    return self.permit2


@internal
def _retain_router(_router: address):
    assert _router != empty(address), BadRouter()
    self.router_refcount[_router] += 1


@internal
def _release_router(_router: address):
    if _router != empty(address):
        self.router_refcount[_router] -= 1


@external
def enable_adapter(_adapter_id: bytes4):
    """
    @notice Enable a registry adapter for the ERC-1271 dispatcher.
    @param _adapter_id Registry adapter identifier.
    """
    assert msg.sender == self._owner(), OnlyOwner()
    assert self.registry.address != empty(address), NoRegistry()
    assert not self.enabled_adapters[_adapter_id], AlreadyEnabled()

    config: adapter_types.AdapterConfig = staticcall self.registry.get_adapter(_adapter_id)
    assert config.validator != empty(address), UnknownAdapter()
    assert config.active, InactiveAdapter()

    router: address = self._resolve_router(config.authorization_mode)
    self.enabled_adapters[_adapter_id] = True
    self.adapter_router[_adapter_id] = router
    if router != empty(address):
        self._retain_router(router)
    log AdapterEnabled(adapter_id=_adapter_id, version=config.version, router=router)


@external
def disable_adapter(_adapter_id: bytes4):
    """
    @notice Disable an adapter; allowed for the owner or the emergency owner.
    @param _adapter_id Registry adapter identifier.
    """
    assert msg.sender in [self._owner(), self._emergency_owner()], OnlyOwner()
    assert self.enabled_adapters[_adapter_id], NotEnabled()

    router: address = self.adapter_router[_adapter_id]
    self.enabled_adapters[_adapter_id] = False
    self.adapter_router[_adapter_id] = empty(address)
    self._release_router(router)
    log AdapterDisabled(adapter_id=_adapter_id, router=router)


@external
def refresh_adapter_router(_adapter_id: bytes4):
    """
    @notice Permissionlessly realign the cached router with the live registry
            config after a registry version update changed the adapter's
            authorization mode.
    @dev Target state is derived from the trusted registry, never
         caller-chosen. Until this call the ERC-1271 dispatcher rejects the
         adapter's signatures, so a mode change can never settle through stale
         allowance state. Follow up with sync_router_approvals for the retired
         router (and the new one, if staging has not run yet).
    @param _adapter_id Registry adapter identifier.
    """
    assert self.enabled_adapters[_adapter_id], NotEnabled()

    config: adapter_types.AdapterConfig = staticcall self.registry.get_adapter(_adapter_id)
    assert config.validator != empty(address), UnknownAdapter()
    assert config.active, InactiveAdapter()

    router: address = self._resolve_router(config.authorization_mode)
    old_router: address = self.adapter_router[_adapter_id]
    if router == old_router:
        return
    self.adapter_router[_adapter_id] = router
    if router != empty(address):
        self._retain_router(router)
    self._release_router(old_router)
    log AdapterRouterRefreshed(
        adapter_id=_adapter_id,
        version=config.version,
        old_router=old_router,
        new_router=router,
    )


# ERC-1271 dispatcher


@internal
@pure
def _decode_normalized_order(
    _response: Bytes[NORMALIZED_ORDER_LEN + 1],
) -> (bool, adapter_types.NormalizedOrder):
    """
    @notice Decode validator return data without reverting on malformed input.
    @dev Manual word extraction instead of abi_decode: dirty address or bool
         words from an untrusted validator must yield an invalid signature,
         never a revert.
    """
    if len(_response) != NORMALIZED_ORDER_LEN:
        return False, empty(adapter_types.NormalizedOrder)

    sell_token_word: uint256 = extract32(_response, 96, output_type=uint256)
    buy_token_word: uint256 = extract32(_response, 128, output_type=uint256)
    receiver_word: uint256 = extract32(_response, 160, output_type=uint256)
    verifier_word: uint256 = extract32(_response, 192, output_type=uint256)
    executor_word: uint256 = extract32(_response, 224, output_type=uint256)
    partial_word: uint256 = extract32(_response, 352, output_type=uint256)
    if (
        sell_token_word >= ADDRESS_BOUND
        or buy_token_word >= ADDRESS_BOUND
        or receiver_word >= ADDRESS_BOUND
        or verifier_word >= ADDRESS_BOUND
        or executor_word >= ADDRESS_BOUND
        or partial_word > 1
    ):
        return False, empty(adapter_types.NormalizedOrder)

    return True, adapter_types.NormalizedOrder(
        recomputed_digest=extract32(_response, 0, output_type=bytes32),
        context_hash=extract32(_response, 32, output_type=bytes32),
        auction_epoch=extract32(_response, 64, output_type=uint256),
        sell_token=convert(convert(sell_token_word, uint160), address),
        buy_token=convert(convert(buy_token_word, uint160), address),
        receiver=convert(convert(receiver_word, uint160), address),
        verifier=convert(convert(verifier_word, uint160), address),
        executor=convert(convert(executor_word, uint160), address),
        sell_amount=extract32(_response, 256, output_type=uint256),
        min_buy_amount=extract32(_response, 288, output_type=uint256),
        valid_to=extract32(_response, 320, output_type=uint256),
        partially_fillable=partial_word == 1,
    )


@internal
@view
def _validate_adapter_signature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4:
    ok: bool = False
    version: uint8 = 0
    adapter_id: bytes4 = empty(bytes4)
    adapter_version: uint16 = 0
    payload: Bytes[adapter_types.MAX_ADAPTER_PAYLOAD] = b""
    ok, version, adapter_id, adapter_version, payload = adapter_types._decode_envelope(
        _signature
    )
    if not ok or version != adapter_types.ENVELOPE_VERSION:
        return INVALID_SIGNATURE
    # enable_adapter requires a nonzero registry, so this also guards the call.
    if not self.enabled_adapters[adapter_id]:
        return INVALID_SIGNATURE

    config: adapter_types.AdapterConfig = staticcall self.registry.get_adapter(adapter_id)
    if not config.active or config.version != adapter_version:
        return INVALID_SIGNATURE
    if config.validator.codehash != config.validator_codehash:
        return INVALID_SIGNATURE
    # Registry-authoritative router consistency: after a registry update
    # changes the authorization mode, signatures stay invalid until the
    # permissionless refresh_adapter_router realigns the cached router and
    # refcounts — a mode change can never settle through stale allowances.
    resolved_ok: bool = False
    resolved_router: address = empty(address)
    resolved_ok, resolved_router = self._resolve_router_lax(config.authorization_mode)
    if not resolved_ok or resolved_router != self.adapter_router[adapter_id]:
        return INVALID_SIGNATURE

    # Untrusted validator: static, non-reverting, exact-size return data.
    # One extra byte of outsize distinguishes oversized return data.
    success: bool = False
    response: Bytes[NORMALIZED_ORDER_LEN + 1] = b""
    success, response = raw_call(
        config.validator,
        abi_encode(
            self, _hash, payload, method_id=method_id("validate(address,bytes32,bytes)")
        ),
        max_outsize=NORMALIZED_ORDER_LEN + 1,
        is_static_call=True,
        revert_on_failure=False,
    )
    if not success:
        return INVALID_SIGNATURE

    decoded: bool = False
    order: adapter_types.NormalizedOrder = empty(adapter_types.NormalizedOrder)
    decoded, order = self._decode_normalized_order(response)
    if not decoded:
        return INVALID_SIGNATURE
    # Layer-local checks: digest identity and adapter-config conformance; the
    # auction's economic checks run behind the hook.
    if order.recomputed_digest != _hash:
        return INVALID_SIGNATURE
    if order.verifier != config.verifier or order.executor != config.executor:
        return INVALID_SIGNATURE
    if order.partially_fillable and not config.allow_partial_fills:
        return INVALID_SIGNATURE
    if not self._check_order_against_lot(order, adapter_id, adapter_version):
        return INVALID_SIGNATURE
    return ERC1271_MAGIC_VALUE


@external
@view
def isValidSignature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4:
    """
    @notice Validate an ERC-1271 signature over a protocol order digest.
    @dev Routing only: a signature starting with the envelope magic takes the
         adapter path where any failure answers 0xffffffff; anything else takes
         the embedded path with its original revert semantics. The nonreentrancy
         lock rejects validation during a native take callback.
    """
    if not adapter_types._has_envelope_magic(_signature):
        return self._validate_embedded_signature(_hash, _signature)
    return self._validate_adapter_signature(_hash, _signature)


# Compile-time integration hooks implemented by the importing contract.
@internal
@view
@abstract
def _owner() -> address: ...


@internal
@view
@abstract
def _emergency_owner() -> address: ...


@internal
@view
@abstract
def _cow_router() -> address: ...


# The auction's payment token: sync_router_approvals refuses to touch its
# allowances, mirroring the core's staging guard.
@internal
@view
@abstract
def _auction_want() -> address: ...


@internal
@view
@abstract
def _validate_embedded_signature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4: ...


# The auction-side economic validation of a decoded adapter order: lot
# activity, amounts, window, live quote, and the replay commitment.
@internal
@view
@abstract
def _check_order_against_lot(
    _order: adapter_types.NormalizedOrder,
    _adapter_id: bytes4,
    _adapter_version: uint16,
) -> bool: ...
