# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Dutch auction ComposableCoW module
@author Curve Finance
@license MIT
@notice Reusable ComposableCoW registration, GPv2 order generation, and validation logic.
@dev The importing burner owns authorization and auction accounting through compile-time hooks.
"""


interface ERC20:
    def approve(_spender: address, _amount: uint256) -> bool: nonpayable
    def allowance(_owner: address, _spender: address) -> uint256: view


struct GPv2Order:
    sellToken: address
    buyToken: address
    receiver: address
    sellAmount: uint256
    buyAmount: uint256
    validTo: uint32
    appData: bytes32
    feeAmount: uint256
    kind: bytes32
    partiallyFillable: bool
    sellTokenBalance: bytes32
    buyTokenBalance: bytes32


struct ConditionalOrderParams:
    handler: address
    salt: bytes32
    staticData: Bytes[STATIC_INPUT_LEN]


struct PayloadStruct:
    proof: DynArray[bytes32, MAX_PROOF_LEN]
    params: ConditionalOrderParams
    offchainInput: Bytes[MAX_OFFCHAIN_INPUT_LEN]


interface ComposableCow:
    def create(_params: ConditionalOrderParams, _dispatch: bool): nonpayable
    def domainSeparator() -> bytes32: view
    def isValidSafeSignature(
        _safe: address,
        _sender: address,
        _hash: bytes32,
        _domain_separator: bytes32,
        _type_hash: bytes32,
        _encoded_order: Bytes[ENCODED_ORDER_LEN],
        _payload: Bytes[MAX_ENCODED_PAYLOAD_LEN],
    ) -> bytes4: view


event CowConfigured:
    composable_cow: indexed(address)
    vault_relayer: indexed(address)
    generation: indexed(uint256)


event CowEnabled:
    generation: indexed(uint256)


event CowDisabled:
    generation: indexed(uint256)


event ConditionalOrderRegistered:
    token: indexed(address)
    generation: indexed(uint256)


event CowAllowanceRevoked:
    token: indexed(address)
    relayer: indexed(address)


# Conditional-order encoding limits
STATIC_INPUT_LEN: constant(uint256) = 52  # packed bytes20 token || bytes32 generation
MAX_HANDLER_INPUT_LEN: constant(uint256) = 256
MAX_OFFCHAIN_INPUT_LEN: constant(uint256) = 256
MAX_PROOF_LEN: constant(uint256) = 32
MAX_SIGNATURE_LEN: constant(uint256) = 2048
ENCODED_ORDER_LEN: constant(uint256) = 12 * 32
MAX_ENCODED_PAYLOAD_LEN: constant(uint256) = 2048
# Finite by construction: a theoretical uint256.max lot leaves one raw unit unsellable.
MAX_COW_BUDGET: public(constant(uint256)) = max_value(uint256) - 1

# GPv2 constants from cowprotocol/contracts@a10f40788af29467e87de3dbf2196662b0a6b500 GPv2Order.
GPV2_ORDER_TYPE_HASH: constant(bytes32) = 0xd5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489
SELL_KIND: public(constant(bytes32)) = 0xf3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775
TOKEN_BALANCE: public(constant(bytes32)) = 0x5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9
ERC1271_MAGIC_VALUE: public(constant(bytes4)) = 0x1626ba7e
CONDITIONAL_ORDER_GENERATOR_INTERFACE: public(constant(bytes4)) = 0xb8296fc4
SIGNATURE_VERIFIER_MUXER_INTERFACE: public(constant(bytes4)) = 0x62af8dc2


# CoW lifecycle state. The importing contract deliberately starts unconfigured and disabled.
cow_enabled: public(bool)
composable_cow: public(ComposableCow)
vault_relayer: public(address)
cow_generation: public(uint256)
registered_generation: public(HashMap[address, uint256])
retired_relayer: public(HashMap[address, bool])


@internal
@pure
def _cow_disabled():
    raw_revert(method_id("CowDisabled()"))


@internal
@pure
def _order_not_valid(_reason: String[32]):
    raw_revert(abi_encode(_reason, method_id=method_id("OrderNotValid(string)")))


@internal
@pure
def _poll_try_at(_timestamp: uint256, _reason: String[32]):
    raw_revert(
        abi_encode(
            _timestamp,
            _reason,
            method_id=method_id("PollTryAtEpoch(uint256,string)"),
        )
    )


@internal
@view
def _assert_cow_enabled():
    if not self.cow_enabled:
        self._cow_disabled()


@internal
@pure
def _encode_static_input(_token: address, _generation: uint256) -> Bytes[STATIC_INPUT_LEN]:
    return concat(convert(_token, bytes20), convert(_generation, bytes32))


@internal
@pure
def _decode_static_input(_static_input: Bytes[MAX_HANDLER_INPUT_LEN]) -> (address, uint256):
    if len(_static_input) != STATIC_INPUT_LEN:
        self._order_not_valid("BadStaticInput")

    token: address = convert(convert(slice(_static_input, 0, 20), bytes20), address)
    generation: uint256 = extract32(_static_input, 20, output_type=uint256)
    if token == empty(address) or self._encode_static_input(token, generation) != _static_input:
        self._order_not_valid("BadStaticInput")
    return token, generation


@internal
@view
def _decode_current_static_input(
    _static_input: Bytes[MAX_HANDLER_INPUT_LEN],
) -> address:
    token: address = empty(address)
    generation: uint256 = 0
    token, generation = self._decode_static_input(_static_input)
    if generation != self.cow_generation:
        self._order_not_valid("StaleGeneration")
    if self.registered_generation[token] != generation:
        self._order_not_valid("OrderNotRegistered")
    return token


@internal
@pure
def _bucket_quote_time(_timestamp: uint256, _start: uint256, _validity: uint256) -> uint256:
    bucket_start: uint256 = _timestamp // _validity * _validity
    return max(bucket_start, _start)


@internal
@pure
def _bucket_valid_to(_timestamp: uint256, _end: uint256, _validity: uint256) -> uint32:
    bucket_end: uint256 = (_timestamp // _validity + 1) * _validity
    return convert(min(bucket_end, _end), uint32)


@internal
@pure
def _order_digest(_order: GPv2Order, _domain_separator: bytes32) -> bytes32:
    struct_hash: bytes32 = keccak256(
        abi_encode(
            GPV2_ORDER_TYPE_HASH,
            _order.sellToken,
            _order.buyToken,
            _order.receiver,
            _order.sellAmount,
            _order.buyAmount,
            _order.validTo,
            _order.appData,
            _order.feeAmount,
            _order.kind,
            _order.partiallyFillable,
            _order.sellTokenBalance,
            _order.buyTokenBalance,
        )
    )
    return keccak256(concat(b"\x19\x01", _domain_separator, struct_hash))


@internal
@view
def _cow_supports_interface(_interface_id: bytes4) -> bool:
    # ComposableCoW probes this interface to distinguish a Safe muxer from an ERC-1271 forwarder.
    assert _interface_id != SIGNATURE_VERIFIER_MUXER_INTERFACE
    return self.cow_enabled and _interface_id in [
        CONDITIONAL_ORDER_GENERATOR_INTERFACE,
        ERC1271_MAGIC_VALUE,
    ]


# Lifecycle functions are internal so the importing burner keeps owner/emergency-owner authorization visible.
@internal
def _configure_cow(_composable_cow: address, _vault_relayer: address):
    assert not self.cow_enabled, "CoW enabled"
    assert _composable_cow != empty(address), "Bad ComposableCoW"
    assert _vault_relayer != empty(address), "Bad vault relayer"
    assert _composable_cow != self.composable_cow.address or _vault_relayer != self.vault_relayer, "Same config"

    previous_relayer: address = self.vault_relayer
    if previous_relayer != empty(address) and previous_relayer != _vault_relayer:
        self.retired_relayer[previous_relayer] = True
    self.retired_relayer[_vault_relayer] = False

    self.composable_cow = ComposableCow(_composable_cow)
    self.vault_relayer = _vault_relayer
    self.cow_generation += 1

    log CowConfigured(
        composable_cow=_composable_cow,
        vault_relayer=_vault_relayer,
        generation=self.cow_generation,
    )


@internal
def _enable_cow():
    assert not self.cow_enabled, "CoW enabled"
    assert self.composable_cow.address != empty(address) and self.vault_relayer != empty(address), "CoW unconfigured"
    self.cow_enabled = True
    log CowEnabled(generation=self.cow_generation)


@internal
def _disable_cow():
    assert self.cow_enabled, "CoW disabled"
    self.cow_enabled = False
    log CowDisabled(generation=self.cow_generation)


@internal
def _set_cow_allowance(_token: address, _spender: address, _amount: uint256):
    # Never approve uint256.max: common ERC-20 implementations treat it as non-decrementing.
    finite_amount: uint256 = min(_amount, MAX_COW_BUDGET)
    token: ERC20 = ERC20(_token)
    if staticcall token.allowance(self, _spender) == finite_amount:
        return
    assert extcall token.approve(_spender, 0, default_return_value=True), "Approve reset failed"
    assert extcall token.approve(_spender, finite_amount, default_return_value=True), "Approve failed"


@internal
@view
def _cow_allowance(_token: address) -> uint256:
    generation: uint256 = self.cow_generation
    if generation == 0 or self.registered_generation[_token] != generation:
        return 0
    return staticcall ERC20(_token).allowance(self, self.vault_relayer)


@internal
def _consume_cow_allowance(_token: address, _amount: uint256):
    # A stale generation has no valid CoW signature and therefore needs no shared-budget update.
    generation: uint256 = self.cow_generation
    if generation == 0 or self.registered_generation[_token] != generation or _amount == 0:
        return

    allowance: uint256 = staticcall ERC20(_token).allowance(self, self.vault_relayer)
    assert allowance >= _amount, "Insufficient CoW budget"
    self._set_cow_allowance(_token, self.vault_relayer, allowance - _amount)


@internal
def _register_cow_order(_token: address, _lot_budget: uint256) -> bool:
    # The target calls this on every COLLECT; creation is once per generation, budget sync is not.
    generation: uint256 = self.cow_generation
    registered: bool = generation != 0 and self.registered_generation[_token] == generation
    if not registered and not self.cow_enabled:
        return False
    assert _token != empty(address) and _token != self._cow_target(), "Bad sell token"

    if registered:
        self._set_cow_allowance(_token, self.vault_relayer, _lot_budget)
        return False

    params: ConditionalOrderParams = ConditionalOrderParams(
        handler=self,
        salt=empty(bytes32),
        staticData=self._encode_static_input(_token, generation),
    )
    extcall self.composable_cow.create(params, True)

    self._set_cow_allowance(_token, self.vault_relayer, _lot_budget)

    self.registered_generation[_token] = generation
    log ConditionalOrderRegistered(token=_token, generation=generation)
    return True


@internal
def _revoke_cow_allowance(_token: address, _relayer: address):
    assert _relayer != empty(address) and self.retired_relayer[_relayer], "Relayer not retired"
    assert _relayer != self.vault_relayer, "Current relayer"
    self._set_cow_allowance(_token, _relayer, 0)
    log CowAllowanceRevoked(token=_token, relayer=_relayer)


@internal
@view
def _canonical_order(_token: address, _sell_amount: uint256) -> GPv2Order:
    active: bool = False
    available: uint256 = 0
    initial_amount: uint256 = 0
    start: uint256 = 0
    end: uint256 = 0
    active, available, initial_amount, start, end = self._cow_order_context(_token)

    if not active or block.timestamp < start or block.timestamp >= end:
        self._poll_try_at(self._cow_next_poll(_token), "NotAllowed")
    if available == 0:
        self._poll_try_at(self._cow_next_poll(_token), "ZeroBalance")

    validity: uint256 = self._cow_order_validity()
    assert validity > 0, "Bad CoW validity"
    quote_time: uint256 = self._bucket_quote_time(block.timestamp, start, validity)
    valid_to: uint32 = self._bucket_valid_to(block.timestamp, end, validity)
    sell_amount: uint256 = min(_sell_amount, available)
    assert sell_amount > 0 and sell_amount <= initial_amount
    buy_amount: uint256 = self._cow_quote(_token, sell_amount, quote_time)
    assert buy_amount > 0, "Zero CoW quote"

    return GPv2Order(
        sellToken=_token,
        buyToken=self._cow_target(),
        receiver=self._cow_receiver(),
        sellAmount=sell_amount,
        buyAmount=buy_amount,
        validTo=valid_to,
        appData=self._cow_app_data(),
        feeAmount=0,
        kind=SELL_KIND,
        partiallyFillable=True,
        sellTokenBalance=TOKEN_BALANCE,
        buyTokenBalance=TOKEN_BALANCE,
    )


@external
@view
def getTradeableOrder(
    _owner: address,
    _sender: address,
    _ctx: bytes32,
    _static_input: Bytes[MAX_HANDLER_INPUT_LEN],
    _offchain_input: Bytes[MAX_OFFCHAIN_INPUT_LEN],
) -> GPv2Order:
    """Generate a canonical, generation-aware GPv2 sell order for a watchtower."""
    self._assert_cow_enabled()
    if _owner != self or len(_offchain_input) != 0:
        self._order_not_valid("BadHandlerInput")

    token: address = self._decode_current_static_input(_static_input)
    return self._canonical_order(token, max_value(uint256))


@external
@view
def verify(
    _owner: address,
    _sender: address,
    _hash: bytes32,
    _domain_separator: bytes32,
    _ctx: bytes32,
    _static_input: Bytes[MAX_HANDLER_INPUT_LEN],
    _offchain_input: Bytes[MAX_OFFCHAIN_INPUT_LEN],
    _order: GPv2Order,
):
    """Validate every economic GPv2 field and reject disabled or stale conditional orders."""
    self._assert_cow_enabled()
    if not self._cow_signature_allowed():
        self._order_not_valid("Reentrancy")
    if _owner != self or len(_offchain_input) != 0:
        self._order_not_valid("BadHandlerInput")

    token: address = self._decode_current_static_input(_static_input)
    active: bool = False
    available: uint256 = 0
    initial_amount: uint256 = 0
    start: uint256 = 0
    end: uint256 = 0
    active, available, initial_amount, start, end = self._cow_order_context(token)
    if not active or block.timestamp < start or block.timestamp >= end:
        self._order_not_valid("NotAllowed")

    domain_separator: bytes32 = staticcall self.composable_cow.domainSeparator()
    if _domain_separator != domain_separator or self._order_digest(_order, domain_separator) != _hash:
        self._order_not_valid("InvalidHash")

    validity: uint256 = self._cow_order_validity()
    if validity == 0:
        self._order_not_valid("BadValidity")
    quote_time: uint256 = self._bucket_quote_time(block.timestamp, start, validity)
    valid_to: uint32 = self._bucket_valid_to(block.timestamp, end, validity)

    if _order.sellToken != token or _order.buyToken != self._cow_target():
        self._order_not_valid("BadToken")
    if _order.receiver != self._cow_receiver() or _order.appData != self._cow_app_data():
        self._order_not_valid("BadReceiverOrAppData")
    if _order.feeAmount != 0 or _order.kind != SELL_KIND or not _order.partiallyFillable:
        self._order_not_valid("BadOrderFlags")
    if _order.sellTokenBalance != TOKEN_BALANCE or _order.buyTokenBalance != TOKEN_BALANCE:
        self._order_not_valid("BadBalanceMode")
    if _order.sellAmount == 0 or _order.sellAmount > initial_amount:
        self._order_not_valid("BadSellAmount")
    if _order.validTo != valid_to or convert(_order.validTo, uint256) <= block.timestamp:
        self._order_not_valid("BadValidTo")
    if _order.buyAmount < self._cow_quote(token, _order.sellAmount, quote_time):
        self._order_not_valid("BadBuyAmount")


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[MAX_SIGNATURE_LEN]) -> bytes4:
    """Validate the GPv2 digest and forward ERC-1271 validation to the configured ComposableCoW."""
    self._assert_cow_enabled()
    if not self._cow_signature_allowed():
        self._order_not_valid("Reentrancy")

    order: GPv2Order = empty(GPv2Order)
    payload: PayloadStruct = empty(PayloadStruct)
    order, payload = abi_decode(_signature, (GPv2Order, PayloadStruct))

    if (
        abi_encode(order, payload) != _signature
        or payload.params.handler != self
        or payload.params.salt != empty(bytes32)
        or len(payload.proof) != 0
        or len(payload.offchainInput) != 0
    ):
        self._order_not_valid("BadSignaturePayload")
    self._decode_current_static_input(payload.params.staticData)

    domain_separator: bytes32 = staticcall self.composable_cow.domainSeparator()
    if self._order_digest(order, domain_separator) != _hash:
        self._order_not_valid("InvalidHash")

    magic: bytes4 = staticcall self.composable_cow.isValidSafeSignature(
        self,
        msg.sender,
        _hash,
        domain_separator,
        empty(bytes32),
        abi_encode(order),
        abi_encode(payload),
    )
    if magic != ERC1271_MAGIC_VALUE:
        self._order_not_valid("InvalidSignature")
    return magic


# Compile-time integration hooks implemented by the target burner.
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
def _cow_app_data() -> bytes32: ...


@internal
@view
@abstract
def _cow_order_validity() -> uint256: ...


@internal
@view
@abstract
def _cow_next_poll(_token: address) -> uint256: ...


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
