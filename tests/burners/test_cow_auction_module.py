from copy import deepcopy

import boa
import pytest
from boa import BoaError
from eth_abi import encode
from eth_hash.auto import keccak


APP_DATA = bytes.fromhex("058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f")
DOMAIN_SEPARATOR = keccak(b"test GPv2 settlement domain")
ORDER_TYPE_HASH = bytes.fromhex("d5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489")
SELL_KIND = bytes.fromhex("f3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775")
TOKEN_BALANCE = bytes.fromhex("5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9")
ZERO_BYTES32 = bytes(32)
MAX_UINT256 = 2**256 - 1
MAX_COW_BUDGET = MAX_UINT256 - 1
DEFAULT_LOT_BUDGET = 150
ORDER_VALIDITY = 120

ORDER_ABI_TYPE = "(address,address,address,uint256,uint256,uint32,bytes32,uint256,bytes32,bool,bytes32,bytes32)"
PAYLOAD_ABI_TYPE = "(bytes32[],(address,bytes32,bytes),bytes)"
ORDER_FIELD_TYPES = [
    "address",
    "address",
    "address",
    "uint256",
    "uint256",
    "uint32",
    "bytes32",
    "uint256",
    "bytes32",
    "bool",
    "bytes32",
    "bytes32",
]


HARNESS_SOURCE = """
# pragma version 0.5.0a4
# pragma nonreentrancy on

import contracts.burners.modules.cow_auction as cow_auction

initializes: cow_auction
exports: (
    cow_auction.getTradeableOrder,
    cow_auction.verify,
    cow_auction.isValidSignature,
    cow_auction.cow_enabled,
    cow_auction.composable_cow,
    cow_auction.vault_relayer,
    cow_auction.cow_generation,
    cow_auction.registered_generation,
    cow_auction.retired_relayer,
    cow_auction.MAX_COW_BUDGET,
)


interface SignatureSelf:
    def isValidSignature(_hash: bytes32, _signature: Bytes[2048]) -> bytes4: view


owner: public(address)
emergency_owner: public(address)
target: public(address)
receiver: public(address)
app_data: public(bytes32)
order_validity: public(uint256)

active: public(bool)
available_amount: public(uint256)
initial_amount: public(uint256)
auction_start: public(uint256)
auction_end: public(uint256)
next_poll: public(uint256)
quote_rate: public(uint256)
signature_allowed: public(bool)


@deploy
def __init__(
    _owner: address,
    _emergency_owner: address,
    _target: address,
    _receiver: address,
    _app_data: bytes32,
    _order_validity: uint256,
):
    self.owner = _owner
    self.emergency_owner = _emergency_owner
    self.target = _target
    self.receiver = _receiver
    self.app_data = _app_data
    self.order_validity = _order_validity
    self.quote_rate = 10
    self.signature_allowed = True


@external
def configure_cow(_composable_cow: address, _vault_relayer: address):
    assert msg.sender == self.owner, "Only owner"
    cow_auction._configure_cow(_composable_cow, _vault_relayer)


@external
def enable_cow():
    assert msg.sender == self.owner, "Only owner"
    cow_auction._enable_cow()


@external
def disable_cow():
    assert msg.sender == self.owner or msg.sender == self.emergency_owner, "Only emergency owner"
    cow_auction._disable_cow()


@external
def register(_token: address, _lot_budget: uint256) -> bool:
    return cow_auction._register_cow_order(_token, _lot_budget)


@external
@view
def cow_allowance(_token: address) -> uint256:
    return cow_auction._cow_allowance(_token)


@external
def consume_cow_allowance(_token: address, _amount: uint256):
    cow_auction._consume_cow_allowance(_token, _amount)


@external
def revoke_cow_allowance(_token: address, _relayer: address):
    assert msg.sender == self.owner or msg.sender == self.emergency_owner, "Only emergency owner"
    cow_auction._revoke_cow_allowance(_token, _relayer)


@external
def set_context(
    _active: bool,
    _available: uint256,
    _initial: uint256,
    _start: uint256,
    _end: uint256,
    _next_poll: uint256,
):
    self.active = _active
    self.available_amount = _available
    self.initial_amount = _initial
    self.auction_start = _start
    self.auction_end = _end
    self.next_poll = _next_poll


@external
def set_signature_allowed(_allowed: bool):
    self.signature_allowed = _allowed


@external
def call_signature_while_locked(_hash: bytes32, _signature: Bytes[2048]) -> bytes4:
    return staticcall SignatureSelf(self).isValidSignature(_hash, _signature)


@external
@view
def supportsInterface(_interface_id: bytes4) -> bool:
    if _interface_id in [0x01ffc9a7, 0xa3b5e311]:
        return True
    return cow_auction._cow_supports_interface(_interface_id)


@override(cow_auction)
@view
def _cow_target() -> address:
    return self.target


@override(cow_auction)
@view
def _cow_receiver() -> address:
    return self.receiver


@override(cow_auction)
@view
def _cow_app_data() -> bytes32:
    return self.app_data


@override(cow_auction)
@view
def _cow_order_validity() -> uint256:
    return self.order_validity


@override(cow_auction)
@view
def _cow_next_poll(_token: address) -> uint256:
    return self.next_poll


@override(cow_auction)
@view
def _cow_order_context(_token: address) -> (bool, uint256, uint256, uint256, uint256):
    available: uint256 = self.available_amount
    if self.active:
        available = min(available, cow_auction._cow_allowance(_token))
    return (
        self.active,
        available,
        self.initial_amount,
        self.auction_start,
        self.auction_end,
    )


@override(cow_auction)
@view
def _cow_quote(_token: address, _sell_amount: uint256, _timestamp: uint256) -> uint256:
    return _sell_amount * self.quote_rate + self.auction_end - _timestamp


@override(cow_auction)
@view
def _cow_signature_allowed() -> bool:
    return self.signature_allowed
"""


COMPOSABLE_COW_SOURCE = """
# pragma version 0.5.0a4

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
    staticData: Bytes[52]

struct PayloadStruct:
    proof: DynArray[bytes32, 32]
    params: ConditionalOrderParams
    offchainInput: Bytes[256]

VERIFY_SELECTOR: constant(bytes4) = 0x14a2a784

domain_separator: public(bytes32)
signature_magic: public(bytes4)
create_count: public(uint256)
last_handler: public(address)
last_salt: public(bytes32)
last_static_data: public(Bytes[52])
last_dispatch: public(bool)

@deploy
def __init__(_domain_separator: bytes32):
    self.domain_separator = _domain_separator
    self.signature_magic = 0x1626ba7e

@external
def create(_params: ConditionalOrderParams, _dispatch: bool):
    self.create_count += 1
    self.last_handler = _params.handler
    self.last_salt = _params.salt
    self.last_static_data = _params.staticData
    self.last_dispatch = _dispatch

@external
@view
def domainSeparator() -> bytes32:
    return self.domain_separator

@external
def set_signature_magic(_magic: bytes4):
    self.signature_magic = _magic

@external
@view
def isValidSafeSignature(
    _safe: address,
    _sender: address,
    _hash: bytes32,
    _domain_separator: bytes32,
    _type_hash: bytes32,
    _encoded_order: Bytes[384],
    _encoded_payload: Bytes[2048],
) -> bytes4:
    order: GPv2Order = abi_decode(_encoded_order, GPv2Order)
    payload: PayloadStruct = abi_decode(_encoded_payload, PayloadStruct)
    success: bool = raw_call(
        payload.params.handler,
        abi_encode(
            _safe,
            _sender,
            _hash,
            _domain_separator,
            empty(bytes32),
            payload.params.staticData,
            payload.offchainInput,
            order,
            method_id=VERIFY_SELECTOR,
        ),
        max_outsize=0,
        is_static_call=True,
        revert_on_failure=False,
    )
    assert success
    return self.signature_magic
"""


TOKEN_SOURCE = """
# pragma version 0.5.0a4

allowance: public(HashMap[address, HashMap[address, uint256]])
balanceOf: public(HashMap[address, uint256])
approve_calls: public(uint256)
return_false: public(bool)
require_reset: public(bool)

@external
def set_return_false(_return_false: bool):
    self.return_false = _return_false

@external
def set_require_reset(_require_reset: bool):
    self.require_reset = _require_reset

@external
def mint(_receiver: address, _amount: uint256):
    self.balanceOf[_receiver] += _amount

@external
def approve(_spender: address, _amount: uint256) -> bool:
    self.approve_calls += 1
    if self.return_false:
        return False
    if self.require_reset and self.allowance[msg.sender][_spender] != 0 and _amount != 0:
        raise "Reset required"
    self.allowance[msg.sender][_spender] = _amount
    return True

@external
def spend_from(_owner: address, _amount: uint256):
    allowance: uint256 = self.allowance[_owner][msg.sender]
    assert allowance >= _amount, "Allowance"
    assert self.balanceOf[_owner] >= _amount, "Balance"
    if allowance != max_value(uint256):
        self.allowance[_owner][msg.sender] = allowance - _amount
    self.balanceOf[_owner] -= _amount
"""


NO_RETURN_TOKEN_SOURCE = """
# pragma version 0.5.0a4

allowance: public(HashMap[address, HashMap[address, uint256]])
approve_calls: public(uint256)

@external
def approve(_spender: address, _amount: uint256):
    self.approve_calls += 1
    self.allowance[msg.sender][_spender] = _amount
"""


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def static_input(token: str, generation: int) -> bytes:
    return bytes.fromhex(token[2:]) + generation.to_bytes(32, "big")


def order_digest(order, domain_separator: bytes = DOMAIN_SEPARATOR) -> bytes:
    struct_hash = keccak(encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def signature_for(order, burner, encoded_static_input: bytes) -> bytes:
    payload = ([], (burner.address, ZERO_BYTES32, encoded_static_input), b"")
    return encode([ORDER_ABI_TYPE, PAYLOAD_ABI_TYPE], [tuple(order), payload])


def order_not_valid(reason: str) -> bytes:
    return selector("OrderNotValid(string)") + encode(["string"], [reason])


def poll_try_at(timestamp: int, reason: str) -> bytes:
    return selector("PollTryAtEpoch(uint256,string)") + encode(
        ["uint256", "string"], [timestamp, reason]
    )


def revert_data(error: BoaError) -> bytes:
    return bytes(error.args[0].output)


@pytest.fixture
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture
def emergency_owner():
    return boa.env.generate_address("emergency_owner")


@pytest.fixture
def unauthorized():
    return boa.env.generate_address("unauthorized")


@pytest.fixture
def receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture
def vault_relayer():
    return boa.env.generate_address("vault_relayer")


@pytest.fixture
def target():
    return boa.env.generate_address("target")


@pytest.fixture
def composable_cow():
    return boa.loads(COMPOSABLE_COW_SOURCE, DOMAIN_SEPARATOR, name="ComposableCowMock")


@pytest.fixture
def token():
    return boa.loads(TOKEN_SOURCE, name="ApprovalToken")


@pytest.fixture
def burner(owner, emergency_owner, target, receiver):
    return boa.loads(
        HARNESS_SOURCE,
        owner,
        emergency_owner,
        target,
        receiver,
        APP_DATA,
        ORDER_VALIDITY,
        name="CowAuctionHarness",
        filename="contracts/testing/CowAuctionHarness.vy",
        no_vvm=True,
    )


def configure_and_register(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    lot_budget=DEFAULT_LOT_BUDGET,
):
    with boa.env.prank(owner):
        burner.configure_cow(composable_cow, vault_relayer)
        burner.enable_cow()
    assert burner.register(token, lot_budget)
    return static_input(token.address, burner.cow_generation())


def set_active_context(burner, available=100, initial=150, duration=1000):
    timestamp = boa.env.evm.vm.state.timestamp
    move = ORDER_VALIDITY - timestamp % ORDER_VALIDITY + 10
    boa.env.time_travel(seconds=move)
    timestamp = boa.env.evm.vm.state.timestamp
    start = timestamp - 10
    end = timestamp + duration
    next_poll = end + 100
    burner.set_context(True, available, initial, start, end, next_poll)
    return timestamp, start, end, next_poll


def test_initial_state_and_lifecycle_authority(
    burner,
    owner,
    emergency_owner,
    unauthorized,
    composable_cow,
    vault_relayer,
):
    assert not burner.cow_enabled()
    assert burner.composable_cow() == "0x0000000000000000000000000000000000000000"
    assert burner.vault_relayer() == "0x0000000000000000000000000000000000000000"
    assert burner.cow_generation() == 0

    with boa.env.prank(unauthorized):
        with boa.reverts("Only owner"):
            burner.configure_cow(composable_cow, vault_relayer)

    with boa.env.prank(owner):
        burner.configure_cow(composable_cow, vault_relayer)
        assert burner.cow_generation() == 1
        burner.enable_cow()
        with boa.reverts("CoW enabled"):
            burner.configure_cow(composable_cow, vault_relayer)

    with boa.env.prank(unauthorized):
        with boa.reverts("Only emergency owner"):
            burner.disable_cow()
    with boa.env.prank(emergency_owner):
        burner.disable_cow()
    assert not burner.cow_enabled()

    with boa.env.prank(owner):
        with boa.reverts("Same config"):
            burner.configure_cow(composable_cow, vault_relayer)
        burner.enable_cow()
    assert burner.cow_generation() == 1


def test_registration_uses_exact_packed_static_input_and_is_idempotent(
    burner, owner, composable_cow, vault_relayer, token
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )

    assert len(encoded_static_input) == 52
    assert composable_cow.last_handler() == burner.address
    assert composable_cow.last_salt() == ZERO_BYTES32
    assert composable_cow.last_static_data() == encoded_static_input
    assert composable_cow.last_dispatch()
    assert composable_cow.create_count() == 1
    assert burner.registered_generation(token) == 1
    assert token.allowance(burner, vault_relayer) == DEFAULT_LOT_BUDGET
    assert burner.cow_allowance(token) == DEFAULT_LOT_BUDGET
    assert token.approve_calls() == 2

    # An unchanged repeated COLLECT neither recreates the order nor touches approval.
    assert not burner.register(token, DEFAULT_LOT_BUDGET)
    assert composable_cow.create_count() == 1
    assert token.allowance(burner, vault_relayer) == DEFAULT_LOT_BUDGET
    assert token.approve_calls() == 2

    assert not burner.register(token, 175)
    assert composable_cow.create_count() == 1
    assert token.allowance(burner, vault_relayer) == 175
    assert token.approve_calls() == 4

    assert not burner.register(token, 175)
    assert token.approve_calls() == 4

    with boa.env.prank(owner):
        burner.disable_cow()
    assert not burner.register(token, 80)
    assert token.allowance(burner, vault_relayer) == 80
    assert token.approve_calls() == 6
    burner.consume_cow_allowance(token, 10)
    assert token.allowance(burner, vault_relayer) == 70
    with boa.env.prank(owner):
        burner.enable_cow()
    assert not burner.register(token, 90)
    assert composable_cow.create_count() == 1
    assert burner.cow_generation() == 1
    assert token.allowance(burner, vault_relayer) == 90
    assert token.approve_calls() == 10


def test_registration_rejects_target_and_rolls_back_failed_approval(
    burner, owner, composable_cow, vault_relayer, token, target
):
    with boa.env.prank(owner):
        burner.configure_cow(composable_cow, vault_relayer)
        burner.enable_cow()

    with boa.reverts("Bad sell token"):
        burner.register(target, DEFAULT_LOT_BUDGET)

    token.set_return_false(True)
    with boa.reverts("Approve reset failed"):
        burner.register(token, DEFAULT_LOT_BUDGET)
    assert composable_cow.create_count() == 0
    assert burner.registered_generation(token) == 0


def test_no_return_approval_is_supported(burner, owner, composable_cow, vault_relayer):
    token = boa.loads(NO_RETURN_TOKEN_SOURCE, name="NoReturnApprovalToken")
    configure_and_register(burner, owner, composable_cow, vault_relayer, token)
    assert token.allowance(burner, vault_relayer) == DEFAULT_LOT_BUDGET
    assert burner.cow_allowance(token) == DEFAULT_LOT_BUDGET
    assert token.approve_calls() == 2

    assert not burner.register(token, 80)
    burner.consume_cow_allowance(token, 30)
    assert token.allowance(burner, vault_relayer) == 50
    assert token.approve_calls() == 6


def test_reset_required_approval_supports_resync_and_native_consumption(
    burner, owner, composable_cow, vault_relayer, token
):
    token.set_require_reset(True)
    configure_and_register(
        burner, owner, composable_cow, vault_relayer, token, lot_budget=100
    )
    assert not burner.register(token, 75)
    burner.consume_cow_allowance(token, 25)
    assert token.allowance(burner, vault_relayer) == 50
    assert token.approve_calls() == 6


def test_budget_is_always_finite_at_uint256_max(
    burner, owner, composable_cow, vault_relayer, token
):
    configure_and_register(
        burner,
        owner,
        composable_cow,
        vault_relayer,
        token,
        lot_budget=MAX_UINT256,
    )
    assert burner.MAX_COW_BUDGET() == MAX_COW_BUDGET
    assert token.allowance(burner, vault_relayer) == MAX_COW_BUDGET
    assert burner.cow_allowance(token) == MAX_COW_BUDGET

    token.mint(burner, 2)
    with boa.env.prank(vault_relayer):
        token.spend_from(burner, 1)
    assert token.allowance(burner, vault_relayer) == MAX_COW_BUDGET - 1


def test_cow_fill_then_donation_cannot_restore_native_budget(
    burner, owner, composable_cow, vault_relayer, token, unauthorized
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token, lot_budget=100
    )
    token.mint(burner, 100)

    with boa.env.prank(vault_relayer):
        token.spend_from(burner, 60)
    assert token.balanceOf(burner) == 40
    assert burner.cow_allowance(token) == 40

    token.mint(burner, 60)  # donation restores live balance, not the shared budget
    assert token.balanceOf(burner) == 100
    assert burner.cow_allowance(token) == 40
    set_active_context(burner, available=100, initial=100)
    order = burner.getTradeableOrder(
        burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
    )
    assert order[3] == 40
    with boa.reverts("Insufficient CoW budget"):
        burner.consume_cow_allowance(token, 41)
    burner.consume_cow_allowance(token, 40)
    assert burner.cow_allowance(token) == 0


def test_native_fill_then_donation_caps_following_cow_fill(
    burner, owner, composable_cow, vault_relayer, token, unauthorized
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token, lot_budget=100
    )
    token.mint(burner, 100)

    burner.consume_cow_allowance(token, 60)
    assert burner.cow_allowance(token) == 40
    token.mint(burner, 60)  # extra inventory cannot increase relayer authorization
    assert token.balanceOf(burner) == 160
    set_active_context(burner, available=100, initial=100)
    order = burner.getTradeableOrder(
        burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
    )
    assert order[3] == 40

    with boa.env.prank(vault_relayer), boa.reverts("Allowance"):
        token.spend_from(burner, 41)
    with boa.env.prank(vault_relayer):
        token.spend_from(burner, 40)
    assert burner.cow_allowance(token) == 0


def test_reconfiguration_retires_relayer_and_requires_new_registration(
    burner,
    owner,
    emergency_owner,
    unauthorized,
    composable_cow,
    vault_relayer,
    token,
):
    old_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    new_composable_cow = boa.loads(
        COMPOSABLE_COW_SOURCE, DOMAIN_SEPARATOR, name="NewComposableCowMock"
    )
    new_relayer = boa.env.generate_address("new_vault_relayer")

    with boa.env.prank(owner):
        burner.disable_cow()
        burner.configure_cow(new_composable_cow, new_relayer)

    assert burner.cow_generation() == 2
    assert burner.retired_relayer(vault_relayer)
    assert not burner.retired_relayer(new_relayer)
    assert burner.registered_generation(token) == 1
    assert burner.cow_allowance(token) == 0
    burner.consume_cow_allowance(token, MAX_UINT256)  # stale generations need no coordination
    assert token.allowance(burner, vault_relayer) == DEFAULT_LOT_BUDGET
    assert not burner.register(token, 200)
    assert new_composable_cow.create_count() == 0
    assert token.allowance(burner, new_relayer) == 0

    with boa.env.prank(owner):
        burner.enable_cow()
    assert burner.register(token, 200)
    assert burner.registered_generation(token) == 2
    assert new_composable_cow.create_count() == 1
    assert token.allowance(burner, new_relayer) == 200

    set_active_context(burner)
    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(burner, unauthorized, ZERO_BYTES32, old_static_input, b"")
    assert revert_data(error.value) == order_not_valid("StaleGeneration")

    with boa.env.prank(unauthorized):
        with boa.reverts("Only emergency owner"):
            burner.revoke_cow_allowance(token, vault_relayer)
    with boa.env.prank(emergency_owner):
        burner.revoke_cow_allowance(token, vault_relayer)
    assert token.allowance(burner, vault_relayer) == 0

    with boa.env.prank(owner):
        with boa.reverts("Relayer not retired"):
            burner.revoke_cow_allowance(token, new_relayer)


def test_interface_behavior_tracks_enabled_state(
    burner, owner, composable_cow, vault_relayer
):
    assert burner.supportsInterface(bytes.fromhex("01ffc9a7"))
    assert burner.supportsInterface(bytes.fromhex("a3b5e311"))
    assert not burner.supportsInterface(bytes.fromhex("b8296fc4"))
    assert not burner.supportsInterface(bytes.fromhex("1626ba7e"))

    with boa.reverts():
        burner.supportsInterface(bytes.fromhex("62af8dc2"))

    with boa.env.prank(owner):
        burner.configure_cow(composable_cow, vault_relayer)
        burner.enable_cow()
    assert burner.supportsInterface(bytes.fromhex("b8296fc4"))
    assert burner.supportsInterface(bytes.fromhex("1626ba7e"))


def test_handler_selectors_are_exact(burner):
    functions = {item["name"]: item for item in burner.abi if item["type"] == "function"}
    assert selector("getTradeableOrder(address,address,bytes32,bytes,bytes)") == bytes.fromhex(
        "b8296fc4"
    )
    assert selector(
        "verify(address,address,bytes32,bytes32,bytes32,bytes,bytes,"
        "(address,address,address,uint256,uint256,uint32,bytes32,uint256,bytes32,bool,bytes32,bytes32))"
    ) == bytes.fromhex("14a2a784")
    assert selector("isValidSignature(bytes32,bytes)") == bytes.fromhex("1626ba7e")
    assert functions["getTradeableOrder"]["stateMutability"] == "view"
    assert functions["verify"]["stateMutability"] == "view"
    assert functions["isValidSignature"]["stateMutability"] == "view"


def test_tradeable_order_fields_and_bucket_stability(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    target,
    receiver,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    timestamp, start, end, _ = set_active_context(burner)
    quote_time = max(timestamp // ORDER_VALIDITY * ORDER_VALIDITY, start)
    expected_valid_to = min(
        (timestamp // ORDER_VALIDITY + 1) * ORDER_VALIDITY,
        end,
    )

    order = burner.getTradeableOrder(
        burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
    )
    assert order[0] == token.address
    assert order[1] == target
    assert order[2] == receiver
    assert order[3] == 100
    assert order[4] == 100 * 10 + end - quote_time
    assert order[5] == expected_valid_to
    assert order[6] == APP_DATA
    assert order[7] == 0
    assert order[8] == SELL_KIND
    assert order[9]
    assert order[10] == TOKEN_BALANCE
    assert order[11] == TOKEN_BALANCE

    boa.env.time_travel(seconds=20)
    assert burner.getTradeableOrder(
        burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
    ) == order

    boa.env.time_travel(seconds=ORDER_VALIDITY)
    next_order = burner.getTradeableOrder(
        burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
    )
    assert next_order[4] < order[4]
    assert next_order[5] > order[5]

    capped_end = boa.env.evm.vm.state.timestamp + 20
    burner.set_context(True, 100, 150, start, capped_end, capped_end + 100)
    end_capped_order = burner.getTradeableOrder(
        burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
    )
    assert end_capped_order[5] == capped_end


@pytest.mark.parametrize(
    "bad_static_input",
    [
        b"",
        bytes(51),
        bytes(53),
        encode(
            ["address", "uint256"],
            ["0x0000000000000000000000000000000000000001", 1],
        ),
    ],
)
def test_static_input_rejects_every_non_52_byte_encoding(
    bad_static_input,
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    configure_and_register(burner, owner, composable_cow, vault_relayer, token)
    set_active_context(burner)
    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, bad_static_input, b""
        )
    assert revert_data(error.value) == order_not_valid("BadStaticInput")


def test_static_input_requires_registration_in_current_generation(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    configure_and_register(burner, owner, composable_cow, vault_relayer, token)
    set_active_context(burner)
    unregistered_token = boa.env.generate_address("unregistered_token")
    unregistered_input = static_input(unregistered_token, burner.cow_generation())

    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, unregistered_input, b""
        )
    assert revert_data(error.value) == order_not_valid("OrderNotRegistered")


def test_tradeable_order_polling_and_disabled_errors(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    timestamp, start, end, next_poll = set_active_context(burner)

    burner.set_context(False, 100, 150, start, end, next_poll)
    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    assert revert_data(error.value) == poll_try_at(next_poll, "NotAllowed")

    burner.set_context(True, 0, 150, start, end, next_poll)
    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    assert revert_data(error.value) == poll_try_at(next_poll, "ZeroBalance")

    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b"unexpected"
        )
    assert revert_data(error.value) == order_not_valid("BadHandlerInput")

    with boa.env.prank(owner):
        burner.disable_cow()
    with pytest.raises(BoaError) as error:
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    assert revert_data(error.value) == selector("CowDisabled()")


def test_verify_checks_all_gpv2_fields_but_not_current_balance(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    timestamp, start, end, _ = set_active_context(burner, available=100, initial=150)
    order = list(
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    )

    burner.verify(
        burner,
        unauthorized,
        order_digest(order),
        DOMAIN_SEPARATOR,
        ZERO_BYTES32,
        encoded_static_input,
        b"",
        order,
    )

    quote_time = max(timestamp // ORDER_VALIDITY * ORDER_VALIDITY, start)
    larger_order = deepcopy(order)
    larger_order[3] = 150
    larger_order[4] = 150 * 10 + end - quote_time
    burner.verify(
        burner,
        unauthorized,
        order_digest(larger_order),
        DOMAIN_SEPARATOR,
        ZERO_BYTES32,
        encoded_static_input,
        b"",
        larger_order,
    )

    mutations = [
        (0, unauthorized, "BadToken"),
        (1, unauthorized, "BadToken"),
        (2, unauthorized, "BadReceiverOrAppData"),
        (6, keccak(b"wrong app data"), "BadReceiverOrAppData"),
        (7, 1, "BadOrderFlags"),
        (8, keccak(b"buy"), "BadOrderFlags"),
        (9, False, "BadOrderFlags"),
        (10, keccak(b"internal"), "BadBalanceMode"),
        (11, keccak(b"external"), "BadBalanceMode"),
        (3, 0, "BadSellAmount"),
        (3, 151, "BadSellAmount"),
        (4, order[4] - 1, "BadBuyAmount"),
        (5, order[5] + 1, "BadValidTo"),
    ]
    for index, value, reason in mutations:
        invalid_order = deepcopy(order)
        invalid_order[index] = value
        with pytest.raises(BoaError) as error:
            burner.verify(
                burner,
                unauthorized,
                order_digest(invalid_order),
                DOMAIN_SEPARATOR,
                ZERO_BYTES32,
                encoded_static_input,
                b"",
                invalid_order,
            )
        assert revert_data(error.value) == order_not_valid(reason)


def test_verify_rejects_hash_domain_state_and_reentrancy_mismatches(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    _, start, end, next_poll = set_active_context(burner)
    order = list(
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    )

    for digest, domain in [
        (keccak(b"wrong hash"), DOMAIN_SEPARATOR),
        (order_digest(order), keccak(b"wrong domain")),
    ]:
        with pytest.raises(BoaError) as error:
            burner.verify(
                burner,
                unauthorized,
                digest,
                domain,
                ZERO_BYTES32,
                encoded_static_input,
                b"",
                order,
            )
        assert revert_data(error.value) == order_not_valid("InvalidHash")

    burner.set_context(False, 100, 150, start, end, next_poll)
    with pytest.raises(BoaError) as error:
        burner.verify(
            burner,
            unauthorized,
            order_digest(order),
            DOMAIN_SEPARATOR,
            ZERO_BYTES32,
            encoded_static_input,
            b"",
            order,
        )
    assert revert_data(error.value) == order_not_valid("NotAllowed")

    burner.set_context(True, 100, 150, start, end, next_poll)
    burner.set_signature_allowed(False)
    with pytest.raises(BoaError) as error:
        burner.verify(
            burner,
            unauthorized,
            order_digest(order),
            DOMAIN_SEPARATOR,
            ZERO_BYTES32,
            encoded_static_input,
            b"",
            order,
        )
    assert revert_data(error.value) == order_not_valid("Reentrancy")


def test_erc1271_forwarding_uses_composable_cow_settlement_domain(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    set_active_context(burner)
    order = list(
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    )
    digest = order_digest(order, composable_cow.domainSeparator())
    signature = signature_for(order, burner, encoded_static_input)

    with boa.env.prank(unauthorized):
        assert burner.isValidSignature(digest, signature) == bytes.fromhex("1626ba7e")

    with pytest.raises(BoaError) as error:
        burner.isValidSignature(order_digest(order, keccak(b"another domain")), signature)
    assert revert_data(error.value) == order_not_valid("InvalidHash")

    composable_cow.set_signature_magic(bytes.fromhex("ffffffff"))
    with pytest.raises(BoaError) as error:
        burner.isValidSignature(digest, signature)
    assert revert_data(error.value) == order_not_valid("InvalidSignature")


def test_erc1271_rejects_stale_or_noncanonical_payload(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    set_active_context(burner)
    order = list(
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    )
    digest = order_digest(order)

    invalid_payloads = [
        ([], (unauthorized, ZERO_BYTES32, encoded_static_input), b""),
        ([], (burner.address, keccak(b"nonzero salt"), encoded_static_input), b""),
        ([keccak(b"proof")], (burner.address, ZERO_BYTES32, encoded_static_input), b""),
        ([], (burner.address, ZERO_BYTES32, encoded_static_input), b"offchain"),
    ]
    for payload in invalid_payloads:
        signature = encode(
            [ORDER_ABI_TYPE, PAYLOAD_ABI_TYPE], [tuple(order), payload]
        )
        with pytest.raises(BoaError) as error:
            burner.isValidSignature(digest, signature)
        assert revert_data(error.value) == order_not_valid("BadSignaturePayload")

    noncanonical_signature = signature_for(order, burner, encoded_static_input) + ZERO_BYTES32
    with pytest.raises(BoaError) as error:
        burner.isValidSignature(digest, noncanonical_signature)
    assert revert_data(error.value) == order_not_valid("BadSignaturePayload")

    stale_signature = signature_for(
        order,
        burner,
        static_input(token.address, burner.cow_generation() + 1),
    )
    with pytest.raises(BoaError) as error:
        burner.isValidSignature(digest, stale_signature)
    assert revert_data(error.value) == order_not_valid("StaleGeneration")


def test_transient_lock_blocks_erc1271_during_mutable_entrypoint(
    burner,
    owner,
    composable_cow,
    vault_relayer,
    token,
    unauthorized,
):
    encoded_static_input = configure_and_register(
        burner, owner, composable_cow, vault_relayer, token
    )
    set_active_context(burner)
    order = list(
        burner.getTradeableOrder(
            burner, unauthorized, ZERO_BYTES32, encoded_static_input, b""
        )
    )
    signature = signature_for(order, burner, encoded_static_input)

    with boa.reverts():
        burner.call_signature_while_locked(order_digest(order), signature)
