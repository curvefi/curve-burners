"""OneInchFeeTakerAdapter: the 1inch FeeTaker-extension adapter behind the
prefix route.

Validation runs end-to-end through the auction's signature router with the
signature `adapter ++ abi_encode(order, extension)`; shape failures revert
with oneinch.OrderNotValid(reason), the auction's typed check_order revert
bubbles unchanged. Settlement is exercised through LimitOrderProtocolMock
and FeeTakerMock (the deployed FeeTaker's math), checking the economic
principle: the fee gross-up only raises what the taker pays, and the
auction's receiver gets at least the linear base of every fill.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.burners.conftest import custom_err
from tests.burners.test_feetaker_module import (
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
    fee_taker_fields,
    gross_up,
)
from tests.burners.test_oneinch_module import (
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
# TakerTraitsLib: the fill amount is a making amount.
TAKER_MAKING_AMOUNT = 1 << 255

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")

PUBLISHED_FLAGS = ALLOW_MULTIPLE_FILLS_FLAG | HAS_EXTENSION_FLAG | POST_INTERACTION_CALL_FLAG
# The fee vector of the tests: 0.5% integrator fee (40% share), 1% resolver
# fee with a 50% whitelist discount.
FEES = (500, 40, 1000, 50)


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
def outsider():
    return boa.env.generate_address("outsider")


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
def fee_taker(fee_taker_deployer, protocol):
    return fee_taker_deployer.deploy(protocol, ZERO_ADDRESS, False)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/oneinch/OneInchFeeTakerAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, protocol, fee_taker):
    return adapter_deployer.deploy(protocol, fee_taker)


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
    # The core fences lots staged in the block that set its economics (the
    # deployment block included): stage from the next second on.
    boa.env.time_travel(seconds=1)
    return harness


@pytest.fixture
def lot(harness, sell_token):
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    return harness.lots(sell_token.address)


def _now() -> int:
    return boa.env.evm.vm.state.timestamp


def _traits(expiration: int, flags: int = PUBLISHED_FLAGS) -> int:
    return flags | (expiration << EXPIRATION_OFFSET)


@pytest.fixture
def make_fields(fee_taker, integrator, protocol_fee_receiver, proceeds_receiver, resolver):
    """The extension fields of a FeeTaker order for the lot: the test fee
    vector, the resolver whitelisted, proceeds to the auction's receiver."""

    def _make(**overrides) -> list[bytes]:
        return fee_taker_fields(
            overrides.pop("fee_taker", fee_taker.address),
            overrides.pop("fees", encode_fees(*FEES)),
            overrides.pop("whitelist", [resolver]),
            overrides.pop("integrator", integrator),
            overrides.pop("protocol", protocol_fee_receiver),
            overrides.pop("custom_receiver", proceeds_receiver),
            **overrides,
        )

    return _make


@pytest.fixture
def extension(make_fields):
    return build_extension(make_fields())


@pytest.fixture
def make_order(harness, sell_token, want, fee_taker, lot):
    """Build an Order tuple over an extension the way a publisher would.

    Overrides address the flat field set (salt, maker, receiver, maker_asset,
    taker_asset, making_amount, taking_amount, maker_traits); `expiration`
    and `flags` build maker_traits when it is not given explicitly, and the
    salt is bound to `extension`.
    """

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
            "receiver": fee_taker.address,
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
    """What takers pass as the fill signature: the protocol forwards it verbatim."""
    return _prefix(adapter) + _bare(order, extension)


def _router_order(order: tuple) -> tuple:
    """The order as the protocol ABI takes it: addresses as uint256 words."""
    return tuple(int(str(field), 16) if isinstance(field, str) else field for field in order)


@pytest.fixture
def validate(harness, adapter, domain_separator):
    """Route an order through the harness: digest under the protocol's
    domain, signature `adapter ++ abi_encode(order, extension)` by default."""

    def _validate(order: tuple, extension: bytes, signature: bytes | None = None):
        signature = _signature(adapter, order, extension) if signature is None else signature
        return harness.isValidSignature(order_digest_reference(order, domain_separator), signature)

    return _validate


@pytest.fixture
def fill(harness, protocol, adapter, want, sell_token, lot):
    """Fill an order through the mock protocol as `taker` (funded and
    approved), the lot approved to the protocol as the registry's executor."""
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


def test_deploy_pins_protocol_domain_and_fee_taker(adapter, protocol, fee_taker, domain_separator):
    assert adapter.limit_order_protocol() == protocol.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert adapter.fee_taker() == fee_taker.address
    assert adapter.ADAPTER_VERSION() == "OneInchFeeTakerAdapter"


def test_deploy_rejects_bad_protocol_or_extension(
    adapter_deployer, fee_taker_deployer, protocol, fee_taker
):
    with boa.reverts(custom_err("BadProtocol()")):
        adapter_deployer.deploy(ZERO_ADDRESS, fee_taker)
    with boa.reverts(custom_err("BadExtensionContract()")):
        adapter_deployer.deploy(protocol, ZERO_ADDRESS)
    with boa.reverts(custom_err("BadExtensionContract()")):
        adapter_deployer.deploy(protocol, boa.env.generate_address("eoa"))
    # A Fusion settlement reads auction details ahead of the fee data: the
    # probe vector is too short for it.
    settlement = fee_taker_deployer.deploy(protocol, ZERO_ADDRESS, True)
    with boa.reverts():
        adapter_deployer.deploy(protocol, settlement)
    # The router itself is a contract but no amount getter.
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


# Shared protocol checks (OneInchAdapter as a module)


def test_shared_checks_apply(adapter, make_order, extension, validate, domain_separator, harness):
    order = make_order(extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        validate(order, extension, _signature(adapter, order, extension) + b"\x00")
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order, extension))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMaker")):
        validate(make_order(extension, maker=boa.env.generate_address("other")), extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(make_order(extension, flags=PUBLISHED_FLAGS | (1 << 248)), extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidExtensionHash")):
        validate(order, extension + b"\x00")
    with boa.reverts(custom_err("OrderNotValid(string)", "MissingOrderExtension")):
        validate(order, b"")


# FeeTaker shape


def test_interaction_flags_and_order_receiver(make_order, extension, validate, proceeds_receiver):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(
            make_order(extension, flags=PUBLISHED_FLAGS & ~POST_INTERACTION_CALL_FLAG), extension
        )
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(
            make_order(extension, flags=PUBLISHED_FLAGS | PRE_INTERACTION_CALL_FLAG), extension
        )
    # The taking tokens must pass through the FeeTaker for the split to run.
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderReceiver")):
        validate(make_order(extension, receiver=proceeds_receiver), extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOrderReceiver")):
        validate(make_order(extension, receiver=ZERO_ADDRESS), extension)


def test_extension_shape_reasons(make_fields, make_order, validate, fee_taker, resolver):
    fields = make_fields()
    other = address_bytes(boa.env.generate_address("other_getter"))
    cases = {
        "BadPreInteraction": {PRE_INTERACTION_DATA: b"\x01"},
        "BadAmountGetter": {MAKING_AMOUNT_DATA: b""},
        "BadAmountGetter ": {TAKING_AMOUNT_DATA: other + fields[3][20:]},
        "BadAmountGetter  ": {TAKING_AMOUNT_DATA: fields[3] + b"\x00"},
        "BadPostInteraction": {POST_INTERACTION_DATA: b""},
        "BadPostInteraction ": {POST_INTERACTION_DATA: other + fields[7][20:]},
        "BadPostInteraction  ": {POST_INTERACTION_DATA: fields[7][:40]},
        # A chained getter or interaction could reprice or redirect.
        "BadGetterTail": {
            MAKING_AMOUNT_DATA: fields[2] + other,
            TAKING_AMOUNT_DATA: fields[3] + other,
        },
        "BadInteractionTail": {POST_INTERACTION_DATA: fields[7] + other},
        "BadInteractionTail ": {POST_INTERACTION_DATA: fields[7] + b"\x00"},
        "BadFeeData": {
            MAKING_AMOUNT_DATA: fields[2][:20] + encode_fees(1, 101, 1, 1) + fields[2][26:],
            TAKING_AMOUNT_DATA: fields[3][:20] + encode_fees(1, 101, 1, 1) + fields[3][26:],
        },
    }
    for reason, mutation in cases.items():
        mutated = list(fields)
        for index, value in mutation.items():
            mutated[index] = value
        extension = build_extension(mutated)
        with boa.reverts(custom_err("OrderNotValid(string)", reason.strip())):
            validate(make_order(extension), extension)
    # Fee data or whitelist differing between the getters and the
    # post-interaction would charge one thing and split another.
    for reason, post in (
        ("BadFeeData", make_fields(fees=encode_fees(500, 40, 1000, 51))[7]),
        ("BadWhitelist", make_fields(whitelist=[])[7]),
        ("BadWhitelist", make_fields(whitelist=[resolver, resolver])[7]),
        ("BadWhitelist", make_fields(whitelist=[boa.env.generate_address("x")])[7]),
    ):
        mutated = list(fields)
        mutated[POST_INTERACTION_DATA] = post
        extension = build_extension(mutated)
        with boa.reverts(custom_err("OrderNotValid(string)", reason)):
            validate(make_order(extension), extension)
    # Predicates and maker permits pass through; the extension size does not
    # matter below the bound.
    decorated = list(fields)
    decorated[4] = b"\xde\xad"
    decorated[5] = b"\xbe\xef"
    extension = build_extension(decorated, custom=b"xyz")
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE


def test_whitelist_and_fee_values_are_not_bounded(make_fields, make_order, validate):
    # Whatever the 1inch API dictates: many resolvers, maximal fees.
    many = [boa.env.generate_address(f"resolver_{i}") for i in range(40)]
    extension = build_extension(
        make_fields(whitelist=many, fees=encode_fees(65535, 100, 65535, 100))
    )
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE
    extension = build_extension(make_fields(whitelist=[], fees=encode_fees()))
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE


def test_custom_receiver_is_the_auction_receiver(make_fields, make_order, validate):
    # No custom receiver: the FeeTaker pays the maker, the auction itself, an
    # accepted receiver (push_target forwards).
    extension = build_extension(make_fields(custom_receiver=None))
    assert validate(make_order(extension), extension) == ERC1271_MAGIC_VALUE
    extension = build_extension(make_fields(custom_receiver=boa.env.generate_address("thief")))
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(extension), extension)


# Economics: the signed amounts priced as a whole by check_order


def test_auction_economics_revert_bubbles_through_adapter(
    harness, make_order, extension, sell_token, validate
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(make_order(extension, taking_amount=quote - 1), extension)
    assert (
        validate(make_order(extension, taking_amount=quote + 1), extension) == ERC1271_MAGIC_VALUE
    )
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(extension, expiration=harness.frame_end() + 1), extension)
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(extension, expiration=0), extension)
    with boa.reverts(custom_err("BadSellAmount()")):
        validate(
            make_order(extension, making_amount=LOT_AMOUNT + 1, taking_amount=quote), extension
        )
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_order(extension, taker_asset=sell_token.address), extension)


# Publisher helpers


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, fee_taker, extension, lot, validate
):
    order, signature = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    order = tuple(order)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert order == (
        bind_salt_reference(SALT, extension),
        harness.address,
        fee_taker.address,
        sell_token.address,
        want.address,
        LOT_AMOUNT,
        quote,
        _traits(harness.frame_end()),
    )
    assert bytes(signature) == _signature(adapter, order, extension)
    assert validate(order, extension, bytes(signature)) == ERC1271_MAGIC_VALUE
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, SALT + 1, LOT_AMOUNT // 4, extension
    )
    assert partial[5] == LOT_AMOUNT // 4
    assert partial[6] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert validate(tuple(partial), extension, bytes(partial_signature)) == ERC1271_MAGIC_VALUE
    # Only the low 96 bits of the salt are the publisher's.
    high, _ = adapter.order_for(
        harness.address, sell_token.address, SALT | (1 << 100), 0, extension
    )
    assert high[0] == order[0]


def test_order_for_checks_the_extension(
    harness, adapter, sell_token, make_fields, extension, erc20_deployer
):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, SALT, 0, extension)
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    for reason, fields in (
        ("BadCustomReceiver", make_fields(custom_receiver=None)),
        ("BadCustomReceiver", make_fields(custom_receiver=boa.env.generate_address("thief"))),
        ("BadFeeData", make_fields(fees=encode_fees(1, 101, 1, 1))),
    ):
        with boa.reverts(custom_err("OrderNotValid(string)", reason)):
            adapter.order_for(harness.address, sell_token.address, SALT, 0, build_extension(fields))


def test_describe_parses_a_publishers_extension(
    adapter, extension, make_fields, integrator, protocol_fee_receiver, proceeds_receiver
):
    fees, receivers, size = adapter.describe(extension)
    assert as_tuple(fees) == FEES
    assert as_tuple(receivers) == (integrator, protocol_fee_receiver, proceeds_receiver)
    assert size == 1
    with boa.reverts(custom_err("OrderNotValid(string)", "BadAmountGetter")):
        adapter.describe(build_extension(make_fields(fee_taker=boa.env.generate_address("x"))))


# Settlement through the mocks: the receiver floor


def test_whitelisted_fill_grosses_up_and_splits(
    harness,
    adapter,
    sell_token,
    want,
    extension,
    resolver,
    proceeds_receiver,
    integrator,
    protocol_fee_receiver,
    fee_taker,
    protocol,
    fill,
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    order = tuple(order)
    making = LOT_AMOUNT // 3
    linear = _linear(order, making)
    # The resolver is whitelisted: the resolver fee is discounted to 0.5%.
    taking = gross_up(linear, 500, 500)
    _making, _taking, _hash = fill(order, extension, resolver, making, TAKER_MAKING_AMOUNT)
    assert (_making, _taking) == (making, taking)
    integrator_amount, protocol_amount = fee_split(taking, 500, 40, 500)
    assert sell_token.balanceOf(resolver) == making
    assert want.balanceOf(integrator) == integrator_amount
    assert want.balanceOf(protocol_fee_receiver) == protocol_amount
    assert want.balanceOf(proceeds_receiver) == taking - integrator_amount - protocol_amount
    assert want.balanceOf(proceeds_receiver) >= linear
    assert want.balanceOf(fee_taker) == 0
    assert harness.available(sell_token.address) == LOT_AMOUNT - making
    assert protocol.remainingInvalidatorForOrder(harness.address, _hash) == LOT_AMOUNT - making


def test_outsider_cannot_fill_without_access(
    harness, adapter, sell_token, extension, outsider, fill
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    with boa.reverts(custom_err("OnlyWhitelistOrAccessToken()")):
        fill(tuple(order), extension, outsider, LOT_AMOUNT // 2, TAKER_MAKING_AMOUNT)


def test_remainder_fills_after_disable_until_the_allowance_sync(
    harness,
    adapter,
    sell_token,
    want,
    extension,
    resolver,
    registry,
    emergency_owner,
    protocol,
    fill,
):
    order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
    order = tuple(order)
    quarter = LOT_AMOUNT // 4
    fill(order, extension, resolver, quarter, TAKER_MAKING_AMOUNT)
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    # Signature checked on the first fill only: the remainder still fills.
    fill(order, extension, resolver, quarter, TAKER_MAKING_AMOUNT)
    assert sell_token.balanceOf(resolver) == 2 * quarter
    fresh, _ = adapter.order_for(harness.address, sell_token.address, SALT + 1, 0, extension)
    with boa.reverts(custom_err("BadSignature()")):
        fill(tuple(fresh), extension, resolver, quarter, TAKER_MAKING_AMOUNT)
    harness.sync_executor_approvals(protocol.address, [sell_token.address])
    assert sell_token.allowance(harness.address, protocol.address) == 0
    with boa.reverts():
        fill(order, extension, resolver, quarter, TAKER_MAKING_AMOUNT)


@given(
    integrator_fee=st.integers(min_value=0, max_value=65535),
    integrator_share=st.integers(min_value=0, max_value=100),
    resolver_fee=st.integers(min_value=0, max_value=65535),
    whitelist_discount=st.integers(min_value=0, max_value=100),
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
    integrator_fee,
    integrator_share,
    resolver_fee,
    whitelist_discount,
    fraction,
    by_making,
):
    with boa.env.anchor():
        fees = encode_fees(integrator_fee, integrator_share, resolver_fee, whitelist_discount)
        extension = build_extension(make_fields(fees=fees))
        order, _ = adapter.order_for(harness.address, sell_token.address, SALT, 0, extension)
        order = tuple(order)
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
