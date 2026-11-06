"""OneInchFusionAdapter: the 1inch Fusion-settlement adapter behind the prefix
route.

Validation runs end-to-end through the auction's signature router with the
signature `adapter ++ abi_encode(order, extension)`; shape failures revert
with oneinch.OrderNotValid(reason), the auction's typed check_order revert
bubbles unchanged. Settlement is exercised through LimitOrderProtocolMock
and FeeTakerMock in Fusion mode (the deployed SimpleSettlement's math),
checking the economic principle: the fee gross-up and the rate bump only
raise what the resolver pays, the surplus fee only takes from the excess
over the estimate, and the auction's receiver gets at least the linear base
of every fill.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.burners.adapters.oneinch.test_module import (
    ALLOW_MULTIPLE_FILLS_FLAG,
    EXPIRATION_OFFSET,
    HAS_EXTENSION_FLAG,
    ORDER_ABI,
    ROUTER_NAME,
    ROUTER_VERSION,
    build_extension,
    domain_separator_reference,
    order_digest_reference,
)
from tests.burners.adapters.oneinch_feetaker.test_module import (
    MAKING_AMOUNT_DATA,
    POST_INTERACTION_CALL_FLAG,
    POST_INTERACTION_DATA,
    PRE_INTERACTION_CALL_FLAG,
    PRE_INTERACTION_DATA,
    TAKING_AMOUNT_DATA,
    address_bytes,
    as_tuple,
    bind_salt_reference,
    encode_fees,
    fee_split,
    gross_up,
)
from tests.burners.adapters.oneinch_fusion.test_module import (
    auction_bump_reference,
    bump_up,
    encode_auction_details,
    encode_surplus,
    fusion_fields,
)
from tests.burners.conftest import custom_err

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
MAX_UINT256 = 2**256 - 1

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
SALT = 0xABCDEF
DOMAIN_FIELDS = b"\x0f"
TAKER_MAKING_AMOUNT = 1 << 255

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")

PUBLISHED_FLAGS = ALLOW_MULTIPLE_FILLS_FLAG | HAS_EXTENSION_FLAG | POST_INTERACTION_CALL_FLAG
FEES = (500, 40, 1000, 50)
# The resolver auction of the tests: 3 minutes from 1% down through two
# points, no gas bump; a 50% surplus fee above the estimate.
AUCTION_DURATION = 180
INITIAL_RATE_BUMP = 100_000
POINTS = [(60_000, 60), (20_000, 60)]
SURPLUS_FEE = 50


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def owner():
    return boa.env.generate_address("owner")


@pytest.fixture(scope="module")
def emergency_owner():
    return boa.env.generate_address("emergency_owner")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("fee_collector")


@pytest.fixture(scope="module")
def integrator():
    return boa.env.generate_address("integrator")


@pytest.fixture(scope="module")
def protocol_fee_receiver():
    return boa.env.generate_address("protocol_fee_receiver")


@pytest.fixture(scope="module")
def resolver():
    return boa.env.generate_address("resolver")


@pytest.fixture(scope="module")
def erc20_deployer():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def want(erc20_deployer):
    return erc20_deployer.deploy("Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def sell_token(erc20_deployer):
    return erc20_deployer.deploy("Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def role_source(owner, emergency_owner):
    return boa.load("contracts/testing/dutch_auction/RoleSourceMock.vy", owner, emergency_owner)


@pytest.fixture(scope="module")
def protocol():
    return boa.load(
        "contracts/testing/dutch_auction/LimitOrderProtocolMock.vy",
        ROUTER_NAME.encode(),
        ROUTER_VERSION.encode(),
        DOMAIN_FIELDS,
        bytes(32),
    )


@pytest.fixture(scope="module")
def domain_separator(protocol):
    return domain_separator_reference(
        ROUTER_NAME, ROUTER_VERSION, boa.env.evm.patch.chain_id, protocol.address
    )


@pytest.fixture(scope="module")
def fee_taker_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/FeeTakerMock.vy")


@pytest.fixture(scope="module")
def settlement(fee_taker_deployer, protocol):
    return fee_taker_deployer.deploy(protocol, ZERO_ADDRESS, True)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/oneinch/OneInchFusionAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, protocol, settlement):
    return adapter_deployer.deploy(protocol, settlement)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, protocol):
    harness = boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        registry.address,
        role_source.address,
        START_TOTAL,
        FLOOR_TOTAL,
        STEP_DURATION,
        AUCTION_LENGTH,
    )
    with boa.env.prank(owner):
        registry.set_adapter(adapter, protocol)
        registry.activate_adapter(adapter)
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    # Anchor the lot window at the current block so auction times are
    # predictable.
    harness.set_frame(_now())
    return harness.lots(sell_token.address)


def _now() -> int:
    return boa.env.evm.vm.state.timestamp


def _traits(expiration: int, flags: int = PUBLISHED_FLAGS) -> int:
    return flags | (expiration << EXPIRATION_OFFSET)


@pytest.fixture
def quote(harness, sell_token, lot):
    return harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)


@pytest.fixture
def make_fields(settlement, integrator, protocol_fee_receiver, proceeds_receiver, resolver, quote):
    """The extension fields of a Fusion order for the lot: an auction
    starting now, the resolver whitelisted from now, the estimate at the
    quote, proceeds to the auction's receiver."""

    def _make(**overrides) -> list[bytes]:
        start = overrides.pop("start", _now())
        auction = overrides.pop(
            "auction",
            encode_auction_details(
                start, overrides.pop("duration", AUCTION_DURATION), INITIAL_RATE_BUMP, POINTS
            ),
        )
        surplus = overrides.pop(
            "surplus", encode_surplus(overrides.pop("estimate", quote), SURPLUS_FEE)
        )
        return fusion_fields(
            overrides.pop("settlement", settlement.address),
            auction,
            overrides.pop("fees", encode_fees(*FEES)),
            overrides.pop("whitelist", [(resolver, 0)]),
            overrides.pop("allowed_time", start),
            overrides.pop("integrator", integrator),
            overrides.pop("protocol", protocol_fee_receiver),
            overrides.pop("custom_receiver", proceeds_receiver),
            surplus,
            **overrides,
        )

    return _make


@pytest.fixture
def extension(make_fields):
    return build_extension(make_fields())


@pytest.fixture
def make_order(harness, sell_token, want, settlement, lot):
    def _make(extension: bytes, **overrides) -> tuple:
        making_amount = overrides.pop("making_amount", LOT_AMOUNT)
        taking_amount = overrides.pop("taking_amount", None)
        if taking_amount is None:
            taking_amount = harness.getAmountNeeded(
                sell_token.address, min(making_amount, LOT_AMOUNT)
            )
        expiration = overrides.pop("expiration", harness.frame_end())
        flags = overrides.pop("flags", PUBLISHED_FLAGS)
        fields = {
            "salt": bind_salt_reference(SALT, extension),
            "maker": harness.address,
            "receiver": settlement.address,
            "maker_asset": sell_token.address,
            "taker_asset": want.address,
            "making_amount": making_amount,
            "taking_amount": taking_amount,
            "maker_traits": _traits(expiration, flags),
        }
        unknown = set(overrides) - set(fields)
        assert not unknown, unknown
        fields.update(overrides)
        return tuple(fields.values())

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple, extension: bytes) -> bytes:
    return encode([ORDER_ABI, "bytes"], [order, extension])


def _signature(adapter: Any, order: tuple, extension: bytes) -> bytes:
    return _prefix(adapter) + _bare(order, extension)


def _router_order(order: tuple) -> tuple:
    return tuple(int(str(field), 16) if isinstance(field, str) else field for field in order)


@pytest.fixture
def validate(harness, adapter, domain_separator):
    def _validate(order: tuple, extension: bytes, signature: bytes | None = None):
        signature = _signature(adapter, order, extension) if signature is None else signature
        return harness.isValidSignature(order_digest_reference(order, domain_separator), signature)

    return _validate


@pytest.fixture
def fill(harness, protocol, adapter, want, sell_token, lot):
    harness.sync_executor_approvals(protocol.address, [sell_token.address])

    def _fill(order: tuple, extension: bytes, taker: str, amount: int, taker_traits: int):
        want._mint_for_testing(taker, 10 * order[6])
        with boa.env.prank(taker):
            want.approve(protocol.address, MAX_UINT256)
            return protocol.fillContractOrderArgs(
                _router_order(order),
                _signature(adapter, order, extension),
                amount,
                taker_traits,
                extension,
            )

    return _fill


def _linear(order: tuple, making: int) -> int:
    return -(-order[6] * making // order[5])


# Deployment pinning


def test_deploy_pins_protocol_domain_and_settlement(
    adapter, protocol, settlement, domain_separator
):
    assert adapter.limit_order_protocol() == protocol.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert adapter.settlement() == settlement.address
    assert adapter.ADAPTER_VERSION() == "OneInchFusionAdapter"


def test_deploy_rejects_bad_protocol_or_extension(
    adapter_deployer, fee_taker_deployer, protocol, settlement
):
    with boa.reverts(custom_err("BadProtocol()")):
        adapter_deployer.deploy(ZERO_ADDRESS, settlement)
    with boa.reverts(custom_err("BadExtensionContract()")):
        adapter_deployer.deploy(protocol, ZERO_ADDRESS)
    with boa.reverts(custom_err("BadExtensionContract()")):
        adapter_deployer.deploy(protocol, boa.env.generate_address("eoa"))
    # A plain FeeTaker reads the probe's auction details as zero fees and
    # prices the vector without the bump.
    fee_taker = fee_taker_deployer.deploy(protocol, ZERO_ADDRESS, False)
    with boa.reverts(custom_err("BadExtensionContract()")):
        adapter_deployer.deploy(protocol, fee_taker)
    with boa.reverts():
        adapter_deployer.deploy(protocol, protocol)


# Routing


def test_prefixed_order_is_valid_and_unprefixed_is_not(adapter, make_order, extension, validate):
    order = make_order(extension)
    assert validate(order, extension) == ERC1271_MAGIC_VALUE
    assert validate(order, extension, _bare(order, extension)) == ERC1271_INVALID


def test_disabled_adapter_stops_routing(
    adapter, make_order, extension, registry, emergency_owner, validate
):
    order = make_order(extension)
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert validate(order, extension) == ERC1271_INVALID


# Fusion shape


def test_interaction_flags_and_order_receiver(make_order, extension, validate, proceeds_receiver):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(
            make_order(extension, flags=PUBLISHED_FLAGS & ~POST_INTERACTION_CALL_FLAG), extension
        )
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(
            make_order(extension, flags=PUBLISHED_FLAGS | PRE_INTERACTION_CALL_FLAG), extension
        )
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderReceiver")):
        validate(make_order(extension, receiver=proceeds_receiver), extension)


def test_extension_shape_reasons(make_fields, make_order, validate, resolver, quote):
    fields = make_fields()
    other = address_bytes(boa.env.generate_address("other"))
    getter = fields[2]
    post = fields[7]
    cases = {
        "BadPreInteraction": {PRE_INTERACTION_DATA: b"\x01"},
        "BadAmountGetter": {MAKING_AMOUNT_DATA: other + getter[20:]},
        "BadAmountGetter ": {TAKING_AMOUNT_DATA: getter + b"\x00"},
        "BadPostInteraction": {POST_INTERACTION_DATA: other + post[20:]},
        # Auction details truncated: the point count overruns the data.
        "BadAuctionDetails": {
            MAKING_AMOUNT_DATA: getter[:37] + b"\x09" + getter[38:],
            TAKING_AMOUNT_DATA: getter[:37] + b"\x09" + getter[38:],
        },
        "BadGetterTail": {MAKING_AMOUNT_DATA: getter + other, TAKING_AMOUNT_DATA: getter + other},
        "BadInteractionTail": {POST_INTERACTION_DATA: post + other},
        "BadInteractionTail ": {POST_INTERACTION_DATA: post + b"\x00"},
        "BadSurplus": {POST_INTERACTION_DATA: post[:-1] + b"\x65"},
        "BadSurplus ": {POST_INTERACTION_DATA: post[:-1]},
    }
    for reason, mutation in cases.items():
        mutated = list(fields)
        for index, value in mutation.items():
            mutated[index] = value
        extension = build_extension(mutated)
        with boa.reverts(custom_err("OrderNotValid(string)", reason.strip())):
            validate(make_order(extension), extension)
    # Fee data or whitelisted takers differing between the getters and the
    # post-interaction.
    for reason, mutated_post in (
        ("BadFeeData", make_fields(fees=encode_fees(500, 41, 1000, 50))[7]),
        ("BadWhitelist", make_fields(whitelist=[])[7]),
        ("BadWhitelist", make_fields(whitelist=[(boa.env.generate_address("x"), 0)])[7]),
    ):
        mutated = list(fields)
        mutated[POST_INTERACTION_DATA] = mutated_post
        extension = build_extension(mutated)
        with boa.reverts(custom_err("OrderNotValid(string)", reason)):
            validate(make_order(extension), extension)
    # Allowed times and delays are the settlement's concern, not the shape's.
    extension = build_extension(
        make_fields(
            whitelist=[(resolver, 60), (boa.env.generate_address("y"), 600)], allowed_time=0
        )
    )
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE


def test_auction_must_end_within_the_order_and_estimate_cover_the_order(
    harness, make_fields, make_order, validate, quote
):
    lot_end = harness.frame_end()
    extension = build_extension(make_fields(start=lot_end - AUCTION_DURATION))
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE
    extension = build_extension(make_fields(start=lot_end - AUCTION_DURATION + 1))
    with boa.reverts(custom_err("OrderNotValid(string)", "AuctionPastExpiration")):
        validate(make_order(extension), extension)
    # An earlier expiration moves the bound with it.
    extension = build_extension(make_fields(start=_now()))
    with boa.reverts(custom_err("OrderNotValid(string)", "AuctionPastExpiration")):
        validate(make_order(extension, expiration=_now() + AUCTION_DURATION - 1), extension)
    assert (
        validate(make_order(extension, expiration=_now() + AUCTION_DURATION), extension)
        == ERC1271_MAGIC_VALUE
    )
    extension = build_extension(make_fields(estimate=quote - 1))
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidEstimatedTakingAmount")):
        validate(make_order(extension), extension)
    # A higher-priced order over the same extension needs a higher estimate.
    extension = build_extension(make_fields(estimate=quote))
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidEstimatedTakingAmount")):
        validate(make_order(extension, taking_amount=quote + 1), extension)
    extension = build_extension(make_fields(estimate=MAX_UINT256))
    assert (
        validate(make_order(extension, taking_amount=quote + 1), extension) == ERC1271_MAGIC_VALUE
    )


def test_custom_receiver_is_the_auction_receiver(make_fields, make_order, validate):
    # No custom receiver: the settlement pays the maker, the auction itself, an
    # accepted receiver (push_target forwards); a third party still fails.
    extension = build_extension(make_fields(custom_receiver=None))
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE
    extension = build_extension(make_fields(custom_receiver=boa.env.generate_address("thief")))
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(extension), extension)


def test_auction_economics_revert_bubbles_through_adapter(
    harness, make_order, extension, sell_token, validate, quote
):
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(make_order(extension, taking_amount=quote - 1), extension)
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(extension, expiration=harness.frame_end() + 1), extension)


# Publisher helpers


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, settlement, extension, make_fields, quote, validate
):
    order, signature = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    order = tuple(order)
    assert order == (
        bind_salt_reference(SALT, extension),
        harness.address,
        settlement.address,
        sell_token.address,
        want.address,
        LOT_AMOUNT,
        quote,
        _traits(harness.frame_end()),
    )
    assert bytes(signature) == _signature(adapter, order, extension)
    assert validate(order, extension, bytes(signature)) == ERC1271_MAGIC_VALUE
    for reason, fields in (
        ("BadCustomReceiver", make_fields(custom_receiver=None)),
        ("AuctionPastExpiration", make_fields(start=harness.frame_end())),
        ("InvalidEstimatedTakingAmount", make_fields(estimate=quote - 1)),
    ):
        with boa.reverts(custom_err("OrderNotValid(string)", reason)):
            adapter.order_for(harness.address, sell_token.address, SALT, 0, build_extension(fields))


def test_describe_parses_a_publishers_extension(
    adapter, extension, integrator, protocol_fee_receiver, proceeds_receiver, quote
):
    fees, receivers, size, details, surplus = adapter.describe(extension)
    assert as_tuple(fees) == FEES
    assert as_tuple(receivers) == (integrator, protocol_fee_receiver, proceeds_receiver)
    assert size == 1
    assert as_tuple(details) == (0, 0, _now(), AUCTION_DURATION, INITIAL_RATE_BUMP, len(POINTS))
    assert as_tuple(surplus) == (quote, SURPLUS_FEE)


# Settlement through the mocks: bump, surplus and the receiver floor


def test_fill_during_the_auction_pays_the_bump_and_shares_the_surplus(
    harness,
    adapter,
    sell_token,
    want,
    extension,
    resolver,
    proceeds_receiver,
    integrator,
    protocol_fee_receiver,
    settlement,
    fill,
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    order = tuple(order)
    start = _now()
    boa.env.time_travel(seconds=30)
    making = LOT_AMOUNT // 2
    linear = _linear(order, making)
    bump = auction_bump_reference(_now(), start, AUCTION_DURATION, INITIAL_RATE_BUMP, POINTS)
    assert 0 < bump < INITIAL_RATE_BUMP
    taking = bump_up(gross_up(linear, 500, 500), bump)
    _making, _taking, _ = fill(order, extension, resolver, making, TAKER_MAKING_AMOUNT)
    assert (_making, _taking) == (making, taking)
    integrator_amount, protocol_amount = fee_split(taking, 500, 40, 500)
    actual = taking - integrator_amount - protocol_amount
    # The estimate is the order's taking amount: scaled to the fill it is
    # the linear base, and the surplus above it is shared 50/50.
    surplus = actual - linear
    assert surplus > 0
    protocol_amount += surplus * SURPLUS_FEE // 100
    assert want.balanceOf(integrator) == integrator_amount
    assert want.balanceOf(protocol_fee_receiver) == protocol_amount
    assert want.balanceOf(proceeds_receiver) == taking - integrator_amount - protocol_amount
    assert want.balanceOf(proceeds_receiver) >= linear
    assert want.balanceOf(settlement) == 0


def test_fill_after_the_auction_pays_the_linear_floor(
    harness, adapter, sell_token, want, extension, resolver, proceeds_receiver, fill
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    order = tuple(order)
    boa.env.time_travel(seconds=AUCTION_DURATION + 1)
    making = LOT_AMOUNT // 2
    linear = _linear(order, making)
    _, taking, _ = fill(order, extension, resolver, making, TAKER_MAKING_AMOUNT)
    assert taking == gross_up(linear, 500, 500)
    # Fees out, no surplus left above the estimate: the receiver gets the
    # base (and a wei of rounding in its favour at most).
    assert linear <= want.balanceOf(proceeds_receiver) <= linear + 1


def test_whitelisted_resolver_waits_for_its_allowed_time(
    harness, adapter, sell_token, make_fields, resolver, fill
):
    # The allowed time gates the first entry; delays gate the following ones.
    extension = build_extension(make_fields(allowed_time=_now() + 60))
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    with boa.reverts(custom_err("AllowedTimeViolation()")):
        fill(tuple(order), extension, resolver, LOT_AMOUNT // 2, TAKER_MAKING_AMOUNT)


@given(
    integrator_fee=st.integers(min_value=0, max_value=65535),
    integrator_share=st.integers(min_value=0, max_value=100),
    resolver_fee=st.integers(min_value=0, max_value=65535),
    whitelist_discount=st.integers(min_value=0, max_value=100),
    initial_bump=st.integers(min_value=0, max_value=2**24 - 1),
    surplus_fee=st.integers(min_value=0, max_value=100),
    estimate_extra=st.integers(min_value=0, max_value=10**24),
    elapsed=st.integers(min_value=0, max_value=AUCTION_DURATION + 10),
    fraction=st.integers(min_value=1, max_value=10**6),
    by_making=st.booleans(),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_receiver_never_gets_less_than_the_linear_base(
    harness,
    adapter,
    sell_token,
    want,
    make_fields,
    resolver,
    proceeds_receiver,
    fill,
    quote,
    integrator_fee,
    integrator_share,
    resolver_fee,
    whitelist_discount,
    initial_bump,
    surplus_fee,
    estimate_extra,
    elapsed,
    fraction,
    by_making,
):
    with boa.env.anchor():
        fees = encode_fees(integrator_fee, integrator_share, resolver_fee, whitelist_discount)
        auction = encode_auction_details(_now(), AUCTION_DURATION, initial_bump, POINTS)
        surplus = encode_surplus(quote + estimate_extra, surplus_fee)
        extension = build_extension(make_fields(fees=fees, auction=auction, surplus=surplus))
        order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
        order = tuple(order)
        boa.env.time_travel(seconds=elapsed)
        before = want.balanceOf(proceeds_receiver)
        if by_making:
            amount = max(1, LOT_AMOUNT * fraction // 10**6)
            making, taking, _ = fill(order, extension, resolver, amount, TAKER_MAKING_AMOUNT)
        else:
            amount = max(1, order[6] * fraction // 10**6)
            making, taking, _ = fill(order, extension, resolver, amount, 0)
        received = want.balanceOf(proceeds_receiver) - before
        assert received >= _linear(order, making)
        assert taking >= received
        assert harness.available(sell_token.address) == LOT_AMOUNT - making
