"""OneInchAdapter: the standalone 1inch LOP v4 adapter behind the prefix
route.

Validation runs end-to-end through the auction's signature router: the order
is handed to takers with signature `adapter ++ abi_encode(order, extension)`,
the harness strips the prefix and forwards the bare pair, the adapter proves
the protocol's order digest, the order shape and the extension policy, and
every economic decision comes from the auction's shared check_order view.
Shape failures revert with the adapter's OrderNotValid(reason); the auction's
typed check_order revert bubbles unchanged for economic ones.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.conftest import custom_err
from tests.burners.test_oneinch_module import (
    ALLOW_MULTIPLE_FILLS_FLAG,
    EXPIRATION_OFFSET,
    NO_PARTIAL_FILLS_FLAG,
    ORDER_ABI,
    ROUTER_NAME,
    ROUTER_VERSION,
    UNWRAP_WETH_FLAG,
    USE_PERMIT2_FLAG,
    bind_extension,
    build_extension,
    domain_separator_reference,
    order_digest_reference,
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
SALT = 0xABCDEF

DOMAIN_FIELDS = b"\x0f"
FOREIGN_DOMAIN = keccak(b"FOREIGN_DOMAIN")

ERC1271_MAGIC_VALUE = bytes.fromhex("1626ba7e")
ERC1271_INVALID = bytes.fromhex("ffffffff")


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
def protocol_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/LimitOrderProtocolMock.vy")


@pytest.fixture(scope="module")
def protocol(protocol_deployer):
    return protocol_deployer.deploy(
        ROUTER_NAME.encode(), ROUTER_VERSION.encode(), DOMAIN_FIELDS, bytes(32)
    )


@pytest.fixture(scope="module")
def domain_separator(protocol):
    return domain_separator_reference(
        ROUTER_NAME, ROUTER_VERSION, boa.env.evm.patch.chain_id, protocol.address
    )


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/oneinch/OneInchAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, protocol):
    return adapter_deployer.deploy(protocol)


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


def _traits(expiration: int, flags: int = ALLOW_MULTIPLE_FILLS_FLAG) -> int:
    return flags | (expiration << EXPIRATION_OFFSET)


@pytest.fixture
def make_order(harness, sell_token, want, proceeds_receiver, lot):
    """Build an Order tuple the way a publisher would.

    Overrides address the flat field set (salt, maker, receiver, maker_asset,
    taker_asset, making_amount, taking_amount, maker_traits); `expiration`
    and `flags` build maker_traits when it is not given explicitly.
    """

    def _make(**overrides) -> tuple:
        making_amount = overrides.pop("making_amount", LOT_AMOUNT)
        taking_amount = overrides.pop("taking_amount", None)
        if taking_amount is None:
            taking_amount = harness.getAmountNeeded(
                sell_token.address, min(making_amount, LOT_AMOUNT)
            )
        expiration = overrides.pop("expiration", harness.frame_end())
        flags = overrides.pop("flags", ALLOW_MULTIPLE_FILLS_FLAG)
        fields = {
            "salt": SALT,
            "maker": harness.address,
            "receiver": proceeds_receiver,
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


def _bare(order: tuple, extension: bytes = b"") -> bytes:
    return encode([ORDER_ABI, "bytes"], [order, extension])


def _signature(adapter: Any, order: tuple, extension: bytes = b"") -> bytes:
    """What takers pass as the fill signature: the protocol forwards it verbatim."""
    return _prefix(adapter) + _bare(order, extension)


@pytest.fixture
def validate(harness, adapter, domain_separator):
    """Route an order through the harness: digest under the protocol's
    domain, signature `adapter ++ abi_encode(order, extension)` by default."""

    def _validate(
        order: tuple,
        signature: bytes | None = None,
        extension: bytes = b"",
        domain: bytes = domain_separator,
    ):
        signature = _signature(adapter, order, extension) if signature is None else signature
        return harness.isValidSignature(order_digest_reference(order, domain), signature)

    return _validate


# Deployment pinning


def test_deploy_pins_protocol_and_domain(adapter, protocol, domain_separator):
    assert adapter.limit_order_protocol() == protocol.address
    assert bytes(adapter.domain_separator()) == domain_separator
    assert bytes(protocol.DOMAIN_SEPARATOR()) == domain_separator
    assert adapter.ADAPTER_VERSION() == "OneInchAdapter"


def test_deploy_rejects_zero_protocol(adapter_deployer):
    with boa.reverts(custom_err("BadProtocol()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_unexpected_domain_fields(protocol_deployer, adapter_deployer):
    with_salt = protocol_deployer.deploy(
        ROUTER_NAME.encode(), ROUTER_VERSION.encode(), b"\x1f", bytes(32)
    )
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(with_salt)


def test_deploy_rejects_a_protocol_hashing_under_another_domain(
    protocol_deployer, adapter_deployer
):
    # eip712Domain() describes one domain, hashOrder uses another (a proxy
    # or a fork with its own separator): the cross-check fails.
    lying = protocol_deployer.deploy(
        ROUTER_NAME.encode(), ROUTER_VERSION.encode(), DOMAIN_FIELDS, FOREIGN_DOMAIN
    )
    with boa.reverts(custom_err("BadDomain()")):
        adapter_deployer.deploy(lying)


# Routing: the 1inch rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order, validate):
    order = make_order()
    assert validate(order) == ERC1271_MAGIC_VALUE
    assert validate(order, _bare(order)) == ERC1271_INVALID


def test_disabled_adapter_stops_routing(
    harness, adapter, make_order, registry, emergency_owner, validate
):
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert validate(order) == ERC1271_INVALID


# Transport encoding


def test_malformed_payloads_fail_decoding(harness, adapter, make_order, validate):
    order = make_order()
    # Truncated, empty, or an extension above the bound: not one pair at all.
    with boa.reverts():
        validate(order, _signature(adapter, order)[:-1])
    with boa.reverts():
        validate(order, _prefix(adapter))
    with boa.reverts():
        validate(order, _prefix(adapter) + _bare(order)[: 8 * 32])
    oversized = bind_extension(order, bytes(2049))
    with boa.reverts():
        validate(oversized, extension=bytes(2049))


def test_non_canonical_encodings_rejected(harness, adapter, make_order, validate):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        validate(order, _signature(adapter, order) + b"\x00")
    bare = _bare(order)
    # Move the extension offset by one word and pad the body so the encoding
    # is loose but still decodes to the same pair.
    loose = bare[: 8 * 32] + (10 * 32).to_bytes(32, "big") + bytes(32) + bare[9 * 32 :]
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        validate(order, _prefix(adapter) + loose)


# Digest and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order, validate):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        validate(order, domain=FOREIGN_DOMAIN)


def test_foreign_maker_invalid(harness, adapter, make_order, validate):
    other_maker = boa.env.generate_address("other_maker")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMaker")):
        validate(make_order(maker=other_maker))


def test_permit2_and_unwrap_traits_invalid(harness, adapter, make_order, validate):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(make_order(flags=ALLOW_MULTIPLE_FILLS_FLAG | USE_PERMIT2_FLAG))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadMakerTraits")):
        validate(make_order(flags=ALLOW_MULTIPLE_FILLS_FLAG | UNWRAP_WETH_FLAG))


def test_fill_mode_traits_are_not_the_adapters_concern(harness, adapter, make_order, validate):
    # Fill-or-kill, single-fill, epoch-checked or sender-restricted orders
    # only restrict who fills and how; the unit price is the same.
    allowed_sender = int(boa.env.generate_address("allowed_sender"), 16) & (2**80 - 1)
    for flags in (0, NO_PARTIAL_FILLS_FLAG, 1 << 250, 1 << 252, 1 << 251):
        assert validate(make_order(flags=flags)) == ERC1271_MAGIC_VALUE
    traits = _traits(harness.frame_end()) | allowed_sender | (7 << 120) | (3 << 160)
    assert validate(make_order(maker_traits=traits)) == ERC1271_MAGIC_VALUE


# Extension policy


def test_extension_binding_is_enforced(harness, adapter, make_order, validate):
    order = make_order()
    extension = build_extension([b""] * 4 + [b"\x01\x02\x03", b"", b"", b""])
    bound = bind_extension(order, extension)
    assert validate(bound, extension=extension) == ERC1271_MAGIC_VALUE
    with boa.reverts(custom_err("OrderNotValid(string)", "UnexpectedOrderExtension")):
        validate(order, extension=extension)
    with boa.reverts(custom_err("OrderNotValid(string)", "MissingOrderExtension")):
        validate(bound)
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidExtensionHash")):
        validate(bound, extension=extension + b"\x00")


def test_amount_getters_and_asset_suffixes_invalid(harness, adapter, make_order, validate):
    getter = bytes.fromhex(boa.env.generate_address("settlement")[2:])
    cases = {
        "BadAssetSuffix": [b"\x01", b"", b"", b"", b"", b"", b"", b""],
        "BadAssetSuffix ": [b"", b"\x01", b"", b"", b"", b"", b"", b""],
        "BadAmountGetter": [b"", b"", getter + b"\x00", b"", b"", b"", b"", b""],
        "BadAmountGetter ": [b"", b"", b"", getter, b"", b"", b"", b""],
    }
    for reason, fields in cases.items():
        extension = build_extension(fields)
        bound = bind_extension(make_order(), extension)
        with boa.reverts(custom_err("OrderNotValid(string)", reason.strip())):
            validate(bound, extension=extension)


def test_restrictive_extension_fields_pass_through(harness, adapter, make_order, validate):
    # A predicate, a maker permit, interactions and custom data never
    # change the price the protocol computes from the order amounts.
    target = bytes.fromhex(boa.env.generate_address("listener")[2:])
    extension = build_extension(
        [b"", b"", b"", b"", b"\xde\xad", b"\xbe\xef", target + b"\x01", target], custom=b"xyz"
    )
    bound = bind_extension(make_order(flags=ALLOW_MULTIPLE_FILLS_FLAG | (1 << 252)), extension)
    assert validate(bound, extension=extension) == ERC1271_MAGIC_VALUE
    # A sub-word extension has every field empty on the protocol side too.
    short = b"\xff" * 31
    bound = bind_extension(make_order(), short)
    assert validate(bound, extension=short) == ERC1271_MAGIC_VALUE


# Economics: linear amounts priced as a whole by check_order


def test_auction_economics_revert_bubbles_through_adapter(
    harness, adapter, make_order, sell_token, validate
):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        validate(make_order(taking_amount=quote - 1))
    assert validate(make_order(taking_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_expiration_is_the_validity_bound(harness, adapter, make_order, validate):
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(expiration=harness.frame_end() + 1))
    # "Never expires" is not an option: the lot window bounds every order.
    with boa.reverts(custom_err("BadValidTo()")):
        validate(make_order(expiration=0))
    assert validate(make_order(expiration=_now())) == ERC1271_MAGIC_VALUE


def test_wrong_taker_asset_or_receiver_invalid(harness, adapter, make_order, sell_token, validate):
    with boa.reverts(custom_err("BadBuyToken()")):
        validate(make_order(taker_asset=sell_token.address))
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(receiver=boa.env.generate_address("thief")))
    with boa.reverts(custom_err("BadReceiver()")):
        validate(make_order(receiver=adapter.address))
    # An empty receiver resolves to the maker, the auction itself, which is an
    # accepted receiver (push_target forwards).
    assert validate(make_order(receiver=ZERO_ADDRESS)) == ERC1271_MAGIC_VALUE


def test_partial_fills_are_priced_against_the_snapshot(
    harness, adapter, make_order, sell_token, want, validate
):
    # Unlike a fill-or-kill rail, a standing ask over the whole lot stays
    # valid after a native take shrinks the remainder: later partial fills
    # keep the unit price and the protocol stops at the balance.
    order = make_order()
    taker = boa.env.generate_address("taker")
    taken = LOT_AMOUNT // 4
    want._mint_for_testing(taker, harness.getAmountNeeded(sell_token.address, taken))
    with boa.env.prank(taker):
        want.approve(harness.address, 2**256 - 1)
        harness.take(sell_token.address, taken)
    assert harness.available(sell_token.address) == LOT_AMOUNT - taken
    assert validate(order) == ERC1271_MAGIC_VALUE
    # The snapshot, not the remainder, bounds the order total.
    with boa.reverts(custom_err("BadSellAmount()")):
        validate(make_order(making_amount=LOT_AMOUNT + 1, taking_amount=order[6]))


# Publisher helper: order + signature for the auction


def test_order_for_builds_the_publishable_pair(
    harness, adapter, sell_token, want, proceeds_receiver, lot, validate
):
    order, signature = adapter.order_for(harness.address, sell_token.address, SALT)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert tuple(order) == (
        SALT,
        harness.address,
        proceeds_receiver,
        sell_token.address,
        want.address,
        LOT_AMOUNT,
        quote,
        _traits(harness.frame_end()),
    )
    # The signature is the documented template and validates end-to-end.
    assert bytes(signature) == _signature(adapter, tuple(order))
    assert validate(tuple(order), bytes(signature)) == ERC1271_MAGIC_VALUE

    # A partial amount is priced proportionally and validates as well.
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, SALT + 1, LOT_AMOUNT // 4
    )
    assert partial[5] == LOT_AMOUNT // 4
    assert partial[6] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert validate(tuple(partial), bytes(partial_signature)) == ERC1271_MAGIC_VALUE


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot, validate):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower with fresh salts rather than cancel.
    harness.set_frame(_now())
    order, signature = adapter.order_for(harness.address, sell_token.address, SALT)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[6]
    assert validate(tuple(order), bytes(signature)) == ERC1271_MAGIC_VALUE


def test_order_for_rejects_empty_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, SALT)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, SALT)
