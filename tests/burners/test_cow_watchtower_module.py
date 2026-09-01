"""Tests for the watchtower publishing shim and the standalone CoW handler.

The old monolithic cow_watchtower module (order building, verification, and
ERC-1271 forwarding inside the burner) was split: the shim only registers
conditional orders with ComposableCoW, while order generation/verification
lives in the standalone, stateless contracts/burners/cow/WatchtowerHandler.vy that
reads the auction's public views.
"""

from copy import deepcopy

import boa
import pytest
from boa import BoaError
from eth_abi import encode
from eth_hash.auto import keccak

from .conftest import custom_err


APP_DATA = bytes.fromhex("058315b749613051abcbf50cf2d605b4fa4a41554ec35d73fd058fc530da559f")
DOMAIN_SEPARATOR = keccak(b"test GPv2 settlement domain")
ORDER_TYPE_HASH = bytes.fromhex("d5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489")
SELL_KIND = bytes.fromhex("f3b277728b3fee749481eb3e0b3b48980dbbab78658fc419025cb16eee346775")
TOKEN_BALANCE = bytes.fromhex("5a28e9363bb942b639270062aa6bb295f434bcdfc42c97267bf003f272060dc9")
ZERO_BYTES32 = bytes(32)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

ORDER_VALIDITY = 120
STEP_DURATION = 60
RAY = 10**27
WAD = 10**18
DECAY_FACTOR_RAY = 9 * RAY // 10

AVAILABLE = 100 * WAD
INITIAL = 150 * WAD
START_TOTAL = 300 * WAD
FLOOR_TOTAL = 30 * WAD

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


SHIM_HARNESS_SOURCE = """
# pragma version 0.5.0b1

import contracts.burners.cow.watchtower as cow_watchtower

initializes: cow_watchtower
exports: (
    cow_watchtower.composable_cow,
    cow_watchtower.cow_handler,
    cow_watchtower.cow_generation,
    cow_watchtower.registered_generation,
)


rail_enabled: public(bool)


@external
def configure(_composable_cow: address, _handler: address):
    cow_watchtower._configure_watchtower(_composable_cow, _handler)


@external
def register(_token: address) -> bool:
    return cow_watchtower._register_cow_order(_token)


@external
def set_rail_enabled(_enabled: bool):
    self.rail_enabled = _enabled


@override(cow_watchtower)
@view
def _cow_rail_enabled() -> bool:
    return self.rail_enabled
"""


# Mock auction exposing every public view the stateless handler reads,
# with setters so tests fully control lot state and configuration.
AUCTION_MOCK_SOURCE = """
# pragma version 0.5.0b1

from contracts.burners.auction import dutch_auction_math as auction_math

struct Lot:
    epoch: uint256
    initial_amount: uint256


cow_enabled: public(bool)
cow_generation: public(uint256)
registered_generation: public(HashMap[address, uint256])
cow_domain_separator: public(bytes32)
cow_order_validity: public(uint256)
app_data: public(bytes32)
want: public(address)
proceeds_receiver: public(address)
start_total: public(uint256)
floor_total: public(uint256)
decay_factor_ray: public(uint256)
step_duration: public(uint256)
lots: public(HashMap[address, Lot])
available: public(HashMap[address, uint256])
next_poll: public(uint256)
# The real auction publishes epoch windows via its calendar, not the lot.
epoch_start: public(HashMap[uint256, uint256])
epoch_end: public(HashMap[uint256, uint256])


@deploy
def __init__(
    _want: address,
    _proceeds_receiver: address,
    _app_data: bytes32,
    _domain_separator: bytes32,
    _order_validity: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    self.want = _want
    self.proceeds_receiver = _proceeds_receiver
    self.app_data = _app_data
    self.cow_domain_separator = _domain_separator
    self.cow_order_validity = _order_validity
    self.decay_factor_ray = _decay_factor_ray
    self.step_duration = _step_duration


@external
@view
def cow_next_poll(_token: address) -> uint256:
    return self.next_poll


# The handler resolves CoW protocol constants through the fallback adapter;
# this mock plays both roles and aliases the adapter views onto itself.
@external
@view
def fallback_adapter() -> address:
    return self


@external
@view
def domain_separator() -> bytes32:
    return self.cow_domain_separator


@external
@view
def order_validity() -> uint256:
    return self.cow_order_validity


@external
def set_enabled(_enabled: bool):
    self.cow_enabled = _enabled


@external
def set_generation(_generation: uint256):
    self.cow_generation = _generation


@external
def set_registered(_token: address, _generation: uint256):
    self.registered_generation[_token] = _generation


@external
def set_domain_separator(_domain_separator: bytes32):
    self.cow_domain_separator = _domain_separator


@external
def set_curve(_decay_factor_ray: uint256, _step_duration: uint256):
    self.decay_factor_ray = _decay_factor_ray
    self.step_duration = _step_duration


@external
def set_lot(
    _token: address,
    _epoch: uint256,
    _initial_amount: uint256,
    _start_total: uint256,
    _floor_total: uint256,
    _start: uint256,
    _end: uint256,
):
    self.lots[_token] = Lot(epoch=_epoch, initial_amount=_initial_amount)
    self.start_total = _start_total
    self.floor_total = _floor_total
    self.epoch_start[_epoch] = _start
    self.epoch_end[_epoch] = _end


@external
@view
def epoch_bounds(_epoch: uint256) -> (uint256, uint256):
    return self.epoch_start[_epoch], self.epoch_end[_epoch]


@external
def set_available(_token: address, _amount: uint256):
    self.available[_token] = _amount


@external
def set_next_poll(_next_poll: uint256):
    self.next_poll = _next_poll


# The handler reads quotes from the auction; the mock prices with the shared
# math so parity tests still pin the exact expected numbers.
@external
@view
def quote(_token: address, _sell_amount: uint256, _ts: uint256) -> uint256:
    lot: Lot = self.lots[_token]
    total: uint256 = auction_math.total_price(
        self.start_total,
        self.floor_total,
        self.decay_factor_ray,
        _ts - self.epoch_start[lot.epoch],
        self.step_duration,
    )
    return auction_math.proportional_payment(total, _sell_amount, lot.initial_amount)
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def static_input(token, generation: int) -> bytes:
    return bytes.fromhex(str(token)[2:]) + generation.to_bytes(32, "big")


def order_digest(order, domain_separator: bytes = DOMAIN_SEPARATOR) -> bytes:
    struct_hash = keccak(encode(["bytes32", *ORDER_FIELD_TYPES], [ORDER_TYPE_HASH, *order]))
    return keccak(b"\x19\x01" + domain_separator + struct_hash)


def order_not_valid(reason: str) -> bytes:
    return selector("OrderNotValid(string)") + encode(["string"], [reason])


def poll_try_at(timestamp: int, reason: str) -> bytes:
    return selector("PollTryAtEpoch(uint256,string)") + encode(
        ["uint256", "string"], [timestamp, reason]
    )


def revert_data(error: BoaError) -> bytes:
    return bytes(error.args[0].output)


def event_name(log) -> str:
    event_type = getattr(log, "event_type", None)
    return event_type.name if event_type is not None else type(log).__name__


# Exact python mirror of contracts/burners/auction/dutch_auction_math.vy so
# handler quotes can be checked for bit-for-bit parity.
def mul_div_up(a: int, b: int, denominator: int) -> int:
    assert denominator != 0
    if a == 0 or b == 0:
        return 0
    return -(-a * b // denominator)


def ray_mul(a: int, b: int) -> int:
    return (a * b + RAY // 2) // RAY


def ray_pow(base_ray: int, exponent: int) -> int:
    result = RAY
    factor = base_ray
    while exponent:
        if exponent & 1:
            result = ray_mul(result, factor)
        exponent >>= 1
        if exponent:
            factor = ray_mul(factor, factor)
    return result


def total_price(start_total, floor_total, decay_factor_ray, elapsed, step_duration):
    steps = elapsed // step_duration
    decayed = start_total * ray_pow(decay_factor_ray, steps) // RAY
    return max(floor_total, decayed)


def expected_buy(
    sell_amount,
    quote_time,
    start,
    initial=INITIAL,
    start_total=START_TOTAL,
    floor_total=FLOOR_TOTAL,
    decay=DECAY_FACTOR_RAY,
    step=STEP_DURATION,
):
    total = total_price(start_total, floor_total, decay, quote_time - start, step)
    return mul_div_up(total, sell_amount, initial)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def sender():
    return boa.env.generate_address("watchtower_sender")


@pytest.fixture
def token():
    return boa.env.generate_address("sell_token")


@pytest.fixture
def want():
    return boa.env.generate_address("want_token")


@pytest.fixture
def proceeds_receiver():
    return boa.env.generate_address("proceeds_receiver")


@pytest.fixture
def handler_address():
    return boa.env.generate_address("cow_handler")


@pytest.fixture
def composable_cow():
    return boa.load(
        "contracts/testing/dutch_auction/ComposableCowMock.vy", name="ComposableCowMock"
    )


@pytest.fixture
def shim():
    return boa.loads(
        SHIM_HARNESS_SOURCE,
        name="CowWatchtowerShimHarness",
        filename="contracts/testing/CowWatchtowerShimHarness.vy",
        no_vvm=True,
    )


@pytest.fixture
def auction(want, proceeds_receiver):
    return boa.loads(
        AUCTION_MOCK_SOURCE,
        want,
        proceeds_receiver,
        APP_DATA,
        DOMAIN_SEPARATOR,
        ORDER_VALIDITY,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
        name="DutchAuctionViewMock",
        no_vvm=True,
    )


@pytest.fixture
def handler():
    return boa.load("contracts/burners/cow/WatchtowerHandler.vy", name="WatchtowerHandler")


def register_token(auction, token, generation=1):
    auction.set_enabled(True)
    auction.set_generation(generation)
    auction.set_registered(token, generation)
    return static_input(token, generation)


def set_live_lot(
    auction,
    token,
    available=AVAILABLE,
    initial=INITIAL,
    start_total=START_TOTAL,
    floor_total=FLOOR_TOTAL,
    duration=1000,
    epoch=1,
):
    """Align just past an ORDER_VALIDITY bucket boundary and open a live lot
    whose start coincides with the bucket start."""
    timestamp = boa.env.evm.vm.state.timestamp
    boa.env.time_travel(seconds=ORDER_VALIDITY - timestamp % ORDER_VALIDITY + 10)
    timestamp = boa.env.evm.vm.state.timestamp
    start = timestamp - 10
    end = timestamp + duration
    next_poll = end + 100
    auction.set_lot(token, epoch, initial, start_total, floor_total, start, end)
    auction.set_available(token, available)
    auction.set_next_poll(next_poll)
    return timestamp, start, end, next_poll


def get_order(handler, auction, sender, encoded_static_input, offchain=b""):
    return list(
        handler.getTradeableOrder(
            auction, sender, ZERO_BYTES32, encoded_static_input, offchain
        )
    )


def verify_order(handler, auction, sender, encoded_static_input, order, digest=None,
                 domain=DOMAIN_SEPARATOR, offchain=b""):
    handler.verify(
        auction,
        sender,
        order_digest(order, domain) if digest is None else digest,
        domain,
        ZERO_BYTES32,
        encoded_static_input,
        offchain,
        order,
    )


# --------------------------------------------------------------------------- #
# Publishing shim
# --------------------------------------------------------------------------- #


def test_shim_configure_rejects_zero_addresses(shim, composable_cow, handler_address):
    with boa.reverts(custom_err("BadComposableCow()")):
        shim.configure(ZERO_ADDRESS, handler_address)
    with boa.reverts(custom_err("BadHandler()")):
        shim.configure(composable_cow, ZERO_ADDRESS)
    assert shim.cow_generation() == 0


def test_shim_configure_bumps_generation_and_emits(shim, composable_cow, handler_address):
    assert shim.composable_cow() == ZERO_ADDRESS
    assert shim.cow_handler() == ZERO_ADDRESS
    assert shim.cow_generation() == 0

    shim.configure(composable_cow, handler_address)
    configured = next(
        log for log in shim.get_logs() if event_name(log) == "WatchtowerConfigured"
    )
    assert configured.composable_cow == composable_cow.address
    assert configured.handler == handler_address
    assert configured.generation == 1

    assert shim.composable_cow() == composable_cow.address
    assert shim.cow_handler() == handler_address
    assert shim.cow_generation() == 1

    new_handler = boa.env.generate_address("new_cow_handler")
    shim.configure(composable_cow, new_handler)
    assert shim.cow_generation() == 2
    assert shim.cow_handler() == new_handler


def test_shim_register_requires_configuration_and_enabled_rail(
    shim, composable_cow, handler_address, token
):
    # Unconfigured (generation zero): registration is silently skipped even
    # with the rail enabled.
    shim.set_rail_enabled(True)
    assert not shim.register(token)
    assert composable_cow.create_count() == 0

    shim.configure(composable_cow, handler_address)
    shim.set_rail_enabled(False)
    assert not shim.register(token)
    assert composable_cow.create_count() == 0
    assert shim.registered_generation(token) == 0

    shim.set_rail_enabled(True)
    assert shim.register(token)
    assert composable_cow.create_count() == 1


def test_shim_register_creates_conditional_order_once(
    shim, composable_cow, handler_address, token
):
    shim.configure(composable_cow, handler_address)
    shim.set_rail_enabled(True)
    assert shim.register(token)
    registered = next(
        log for log in shim.get_logs() if event_name(log) == "ConditionalOrderRegistered"
    )
    assert registered.token == token
    assert registered.generation == 1

    encoded_static_input = static_input(token, 1)
    assert len(encoded_static_input) == 52
    assert composable_cow.create_count() == 1
    assert composable_cow.last_owner() == shim.address
    assert composable_cow.last_handler() == handler_address
    assert composable_cow.last_salt() == ZERO_BYTES32
    assert composable_cow.last_static_data() == encoded_static_input
    assert composable_cow.last_dispatch()
    assert shim.registered_generation(token) == 1

    # Repeated staging never recreates the conditional order within the same
    # generation, regardless of rail toggling in between.
    assert not shim.register(token)
    shim.set_rail_enabled(False)
    assert not shim.register(token)
    shim.set_rail_enabled(True)
    assert not shim.register(token)
    assert composable_cow.create_count() == 1

    # Other tokens register independently.
    other_token = boa.env.generate_address("other_sell_token")
    assert shim.register(other_token)
    assert composable_cow.create_count() == 2
    assert composable_cow.last_static_data() == static_input(other_token, 1)


def test_shim_reconfiguration_requires_new_registration(
    shim, composable_cow, handler_address, token
):
    shim.configure(composable_cow, handler_address)
    shim.set_rail_enabled(True)
    assert shim.register(token)

    new_composable_cow = boa.load(
        "contracts/testing/dutch_auction/ComposableCowMock.vy", name="NewComposableCowMock"
    )
    new_handler = boa.env.generate_address("new_cow_handler")
    shim.configure(new_composable_cow, new_handler)
    assert shim.cow_generation() == 2
    assert shim.registered_generation(token) == 1

    assert shim.register(token)
    assert shim.registered_generation(token) == 2
    assert new_composable_cow.create_count() == 1
    assert new_composable_cow.last_handler() == new_handler
    assert new_composable_cow.last_static_data() == static_input(token, 2)
    assert composable_cow.create_count() == 1

    assert not shim.register(token)
    assert new_composable_cow.create_count() == 1


# --------------------------------------------------------------------------- #
# Standalone handler: interface
# --------------------------------------------------------------------------- #


def test_handler_interface_and_selectors(handler):
    assert handler.supportsInterface(bytes.fromhex("01ffc9a7"))
    assert handler.supportsInterface(bytes.fromhex("b8296fc4"))
    assert not handler.supportsInterface(bytes.fromhex("1626ba7e"))
    assert not handler.supportsInterface(bytes.fromhex("62af8dc2"))
    assert not handler.supportsInterface(bytes.fromhex("ffffffff"))

    assert selector("getTradeableOrder(address,address,bytes32,bytes,bytes)") == bytes.fromhex(
        "b8296fc4"
    )
    assert selector(
        "verify(address,address,bytes32,bytes32,bytes32,bytes,bytes,"
        "(address,address,address,uint256,uint256,uint32,bytes32,uint256,bytes32,bool,bytes32,bytes32))"
    ) == bytes.fromhex("14a2a784")

    functions = {item["name"]: item for item in handler.abi if item["type"] == "function"}
    assert functions["getTradeableOrder"]["stateMutability"] == "view"
    assert functions["verify"]["stateMutability"] == "view"
    assert functions["supportsInterface"]["stateMutability"] == "view"


# --------------------------------------------------------------------------- #
# Standalone handler: getTradeableOrder
# --------------------------------------------------------------------------- #


def test_tradeable_order_fields_and_bucket_stability(
    handler, auction, token, want, proceeds_receiver, sender
):
    encoded_static_input = register_token(auction, token)
    timestamp, start, end, _ = set_live_lot(auction, token)
    quote_time = max(timestamp // ORDER_VALIDITY * ORDER_VALIDITY, start)
    expected_valid_to = min((timestamp // ORDER_VALIDITY + 1) * ORDER_VALIDITY, end)

    order = get_order(handler, auction, sender, encoded_static_input)
    assert order[0] == token
    assert order[1] == want
    assert order[2] == proceeds_receiver
    assert order[3] == AVAILABLE
    assert order[4] == expected_buy(AVAILABLE, quote_time, start)
    assert order[5] == expected_valid_to
    assert order[6] == APP_DATA
    assert order[7] == 0
    assert order[8] == SELL_KIND
    assert order[9]
    assert order[10] == TOKEN_BALANCE
    assert order[11] == TOKEN_BALANCE

    # Quotes are stable within a validity bucket.
    boa.env.time_travel(seconds=20)
    assert get_order(handler, auction, sender, encoded_static_input) == order

    # The next bucket advances the decay steps (cheaper) and validTo.
    boa.env.time_travel(seconds=ORDER_VALIDITY)
    next_order = get_order(handler, auction, sender, encoded_static_input)
    assert next_order[4] == expected_buy(AVAILABLE, quote_time + ORDER_VALIDITY, start)
    assert next_order[4] < order[4]
    assert next_order[5] == order[5] + ORDER_VALIDITY

    # validTo is clamped by the lot end inside the final bucket.
    capped_end = boa.env.evm.vm.state.timestamp + 20
    auction.set_lot(token, 1, INITIAL, START_TOTAL, FLOOR_TOTAL, start, capped_end)
    end_capped_order = get_order(handler, auction, sender, encoded_static_input)
    assert end_capped_order[5] == capped_end


def test_tradeable_order_quote_parity_across_buckets_and_floor(
    handler, auction, token, sender
):
    # Deliberately awkward numbers so ceil rounding in every mul_div_up shows.
    available = 7 * WAD + 3
    initial = 11 * WAD + 7
    start_total = 5 * WAD + 1
    floor_total = start_total // 1000
    decay = RAY // 2

    auction.set_curve(decay, STEP_DURATION)
    encoded_static_input = register_token(auction, token)
    _, start, _, _ = set_live_lot(
        auction,
        token,
        available=available,
        initial=initial,
        start_total=start_total,
        floor_total=floor_total,
        duration=100 * ORDER_VALIDITY,
    )

    for _ in range(5):
        timestamp = boa.env.evm.vm.state.timestamp
        quote_time = max(timestamp // ORDER_VALIDITY * ORDER_VALIDITY, start)
        order = get_order(handler, auction, sender, encoded_static_input)
        assert order[3] == available
        assert order[4] == expected_buy(
            available,
            quote_time,
            start,
            initial=initial,
            start_total=start_total,
            floor_total=floor_total,
            decay=decay,
        )
        boa.env.time_travel(seconds=ORDER_VALIDITY)

    # Far enough along the curve the decayed price clamps to the floor.
    boa.env.time_travel(seconds=30 * ORDER_VALIDITY)
    floor_order = get_order(handler, auction, sender, encoded_static_input)
    assert floor_order[4] == mul_div_up(floor_total, available, initial)


def test_tradeable_order_zero_quote_reverts(handler, auction, token, sender):
    encoded_static_input = register_token(auction, token)
    set_live_lot(auction, token, start_total=0, floor_total=0)
    with boa.reverts(custom_err("ZeroQuote()")):
        get_order(handler, auction, sender, encoded_static_input)


@pytest.mark.parametrize(
    "bad_static_input",
    [
        b"",
        bytes(51),
        bytes(53),
        bytes(52),  # zero token address
        encode(["address", "uint256"], ["0x0000000000000000000000000000000000000001", 1]),
    ],
)
def test_static_input_rejects_every_non_canonical_encoding(
    bad_static_input, handler, auction, token, sender
):
    register_token(auction, token)
    set_live_lot(auction, token)
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, bad_static_input)
    assert revert_data(error.value) == order_not_valid("BadStaticInput")


def test_static_input_generation_and_registration_checks(handler, auction, token, sender):
    encoded_static_input = register_token(auction, token)
    set_live_lot(auction, token)

    # Stale generation: the auction rewired its watchtower since registration.
    auction.set_generation(2)
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input)
    assert revert_data(error.value) == order_not_valid("StaleGeneration")

    with pytest.raises(BoaError) as error:
        verify_order(
            handler,
            auction,
            sender,
            encoded_static_input,
            [token, token, token, 1, 1, 1, ZERO_BYTES32, 0, SELL_KIND, True,
             TOKEN_BALANCE, TOKEN_BALANCE],
        )
    assert revert_data(error.value) == order_not_valid("StaleGeneration")
    auction.set_generation(1)

    # Registered-generation mismatch for an unregistered token.
    unregistered_token = boa.env.generate_address("unregistered_token")
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, static_input(unregistered_token, 1))
    assert revert_data(error.value) == order_not_valid("OrderNotRegistered")


def test_tradeable_order_polling_disabled_and_offchain_errors(
    handler, auction, token, sender
):
    encoded_static_input = register_token(auction, token)
    timestamp, start, end, next_poll = set_live_lot(auction, token)

    # No lot at all.
    auction.set_lot(token, 0, 0, 0, 0, 0, 0)
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input)
    assert revert_data(error.value) == poll_try_at(next_poll, "NotAllowed")

    # Lot not started yet.
    auction.set_lot(
        token, 1, INITIAL, START_TOTAL, FLOOR_TOTAL, timestamp + 100, end
    )
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input)
    assert revert_data(error.value) == poll_try_at(next_poll, "NotAllowed")

    # Lot already over.
    auction.set_lot(
        token, 1, INITIAL, START_TOTAL, FLOOR_TOTAL, start, timestamp
    )
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input)
    assert revert_data(error.value) == poll_try_at(next_poll, "NotAllowed")

    # Live lot with nothing available.
    auction.set_lot(token, 1, INITIAL, START_TOTAL, FLOOR_TOTAL, start, end)
    auction.set_available(token, 0)
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input)
    assert revert_data(error.value) == poll_try_at(next_poll, "ZeroBalance")
    auction.set_available(token, AVAILABLE)

    # Offchain input must stay empty.
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input, offchain=b"unexpected")
    assert revert_data(error.value) == order_not_valid("BadHandlerInput")

    # Rail switched off.
    auction.set_enabled(False)
    with pytest.raises(BoaError) as error:
        get_order(handler, auction, sender, encoded_static_input)
    assert revert_data(error.value) == selector("CowDisabled()")


# --------------------------------------------------------------------------- #
# Standalone handler: verify
# --------------------------------------------------------------------------- #


def test_verify_accepts_canonical_and_partially_filled_orders(
    handler, auction, token, sender
):
    encoded_static_input = register_token(auction, token)
    timestamp, start, _, _ = set_live_lot(auction, token)
    quote_time = max(timestamp // ORDER_VALIDITY * ORDER_VALIDITY, start)
    order = get_order(handler, auction, sender, encoded_static_input)

    verify_order(handler, auction, sender, encoded_static_input, order)

    # A partially filled order keeps its original sellAmount above the live
    # availability; verify bounds it by the lot snapshot, not by available().
    larger_order = deepcopy(order)
    larger_order[3] = INITIAL
    larger_order[4] = expected_buy(INITIAL, quote_time, start)
    verify_order(handler, auction, sender, encoded_static_input, larger_order)

    # Overpaying relative to the curve quote is always acceptable.
    overpriced_order = deepcopy(order)
    overpriced_order[4] = order[4] + 1
    verify_order(handler, auction, sender, encoded_static_input, overpriced_order)


def test_verify_checks_all_gpv2_fields(handler, auction, token, sender):
    encoded_static_input = register_token(auction, token)
    _, _, _, _ = set_live_lot(auction, token)
    order = get_order(handler, auction, sender, encoded_static_input)

    mutations = [
        (0, sender, "BadToken"),
        (1, sender, "BadToken"),
        (2, sender, "BadReceiverOrAppData"),
        (6, keccak(b"wrong app data"), "BadReceiverOrAppData"),
        (7, 1, "BadOrderFlags"),
        (8, keccak(b"buy"), "BadOrderFlags"),
        (9, False, "BadOrderFlags"),
        (10, keccak(b"internal"), "BadBalanceMode"),
        (11, keccak(b"external"), "BadBalanceMode"),
        (3, 0, "BadSellAmount"),
        (3, INITIAL + 1, "BadSellAmount"),
        (4, order[4] - 1, "BadBuyAmount"),
        (5, order[5] + 1, "BadValidTo"),
        (5, order[5] - 1, "BadValidTo"),
    ]
    for index, value, reason in mutations:
        invalid_order = deepcopy(order)
        invalid_order[index] = value
        with pytest.raises(BoaError) as error:
            verify_order(handler, auction, sender, encoded_static_input, invalid_order)
        assert revert_data(error.value) == order_not_valid(reason)


def test_verify_rejects_hash_domain_and_state_mismatches(handler, auction, token, sender):
    encoded_static_input = register_token(auction, token)
    timestamp, start, end, next_poll = set_live_lot(auction, token)
    order = get_order(handler, auction, sender, encoded_static_input)

    # Wrong digest for the order, then wrong settlement domain.
    with pytest.raises(BoaError) as error:
        verify_order(
            handler, auction, sender, encoded_static_input, order,
            digest=keccak(b"wrong hash"),
        )
    assert revert_data(error.value) == order_not_valid("InvalidHash")

    with pytest.raises(BoaError) as error:
        verify_order(
            handler, auction, sender, encoded_static_input, order,
            domain=keccak(b"wrong domain"),
        )
    assert revert_data(error.value) == order_not_valid("InvalidHash")

    # Inactive lot states report NotAllowed (a signature validity answer, not
    # a polling hint like getTradeableOrder gives).
    auction.set_lot(token, 0, 0, 0, 0, 0, 0)
    with pytest.raises(BoaError) as error:
        verify_order(handler, auction, sender, encoded_static_input, order)
    assert revert_data(error.value) == order_not_valid("NotAllowed")

    auction.set_lot(
        token, 1, INITIAL, START_TOTAL, FLOOR_TOTAL, timestamp + 100, end
    )
    with pytest.raises(BoaError) as error:
        verify_order(handler, auction, sender, encoded_static_input, order)
    assert revert_data(error.value) == order_not_valid("NotAllowed")

    auction.set_lot(token, 1, INITIAL, START_TOTAL, FLOOR_TOTAL, start, end)
    auction.set_available(token, 0)
    with pytest.raises(BoaError) as error:
        verify_order(handler, auction, sender, encoded_static_input, order)
    assert revert_data(error.value) == order_not_valid("NotAllowed")
    auction.set_available(token, AVAILABLE)

    # Offchain input must stay empty in verify as well.
    with pytest.raises(BoaError) as error:
        verify_order(
            handler, auction, sender, encoded_static_input, order, offchain=b"offchain"
        )
    assert revert_data(error.value) == order_not_valid("BadHandlerInput")

    # Rail switched off.
    auction.set_enabled(False)
    with pytest.raises(BoaError) as error:
        verify_order(handler, auction, sender, encoded_static_input, order)
    assert revert_data(error.value) == selector("CowDisabled()")


def test_handler_is_stateless_across_auctions(
    handler, auction, token, sender, proceeds_receiver
):
    # The same handler deployment serves any auction passed as the conditional
    # order owner; quotes come solely from that owner's views.
    encoded_static_input = register_token(auction, token)
    set_live_lot(auction, token)
    order = get_order(handler, auction, sender, encoded_static_input)

    other_want = boa.env.generate_address("other_want")
    other_auction = boa.loads(
        AUCTION_MOCK_SOURCE,
        other_want,
        proceeds_receiver,
        APP_DATA,
        DOMAIN_SEPARATOR,
        ORDER_VALIDITY,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
        name="OtherDutchAuctionViewMock",
        no_vvm=True,
    )
    other_input = register_token(other_auction, token)
    other_auction.set_lot(
        token,
        1,
        INITIAL,
        START_TOTAL,
        FLOOR_TOTAL,
        boa.env.evm.vm.state.timestamp - 10,
        boa.env.evm.vm.state.timestamp + 1000,
    )
    other_auction.set_available(token, AVAILABLE // 2)
    other_order = get_order(handler, other_auction, sender, other_input)

    assert order[1] != other_order[1]
    assert other_order[1] == other_want
    assert other_order[3] == AVAILABLE // 2
    # The first auction's order is untouched by the second owner's state.
    assert get_order(handler, auction, sender, encoded_static_input) == order
