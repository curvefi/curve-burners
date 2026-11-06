"""UniswapXAdapter: the standalone ExclusiveDutchOrderReactor adapter behind
the prefix route.

Validation runs end-to-end through the auction's signature router: the order
is handed to fillers with signature `adapter ++ abi_encode(order)`, the
harness strips the prefix and forwards the bare order, the adapter proves the
Permit2 witness digest and the order shape, replicates the reactor's decay,
and every economic decision comes from the auction's shared check_order view.
Shape failures revert with the adapter's OrderNotValid(reason); the auction's
typed check_order revert bubbles unchanged for economic ones.
"""

from typing import Any

import boa
import pytest
from eth_abi import encode
from eth_utils import keccak

from tests.burners.adapters.uniswapx.test_module import ORDER_ABI, permit_digest_reference
from tests.burners.conftest import custom_err

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
AUCTION_LENGTH = 24 * 60 * 60
LOT_AMOUNT = 250 * WAD
NONCE = 0xABCDEF

DOMAIN_SEPARATOR = keccak(b"PERMIT2_TEST_DOMAIN")
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
def permit2_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/Permit2Mock.vy")


@pytest.fixture(scope="module")
def reactor_deployer():
    return boa.load_partial("contracts/testing/dutch_auction/ReactorMock.vy")


@pytest.fixture(scope="module")
def permit2(permit2_deployer):
    return permit2_deployer.deploy(DOMAIN_SEPARATOR)


@pytest.fixture(scope="module")
def reactor(reactor_deployer, permit2):
    return reactor_deployer.deploy(permit2)


@pytest.fixture(scope="module")
def adapter_deployer():
    return boa.load_partial("contracts/burners/adapters/uniswapx/UniswapXAdapter.vy")


@pytest.fixture
def registry(role_source):
    return boa.load("contracts/burners/adapters/AdapterRegistry.vy", role_source.address)


@pytest.fixture
def adapter(adapter_deployer, reactor):
    return adapter_deployer.deploy(reactor)


@pytest.fixture
def harness(role_source, want, proceeds_receiver, registry, adapter, owner, permit2):
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
        registry.set_adapter(adapter, permit2)
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


@pytest.fixture
def make_order(harness, reactor, sell_token, want, proceeds_receiver, lot):
    """Build an ExclusiveDutchOrder tuple the way a publisher would.

    Overrides address the flat field set: info fields (reactor, swapper,
    nonce, deadline, validation_contract, validation_data), the decay window
    (decay_start, decay_end), exclusivity (exclusive_filler, override_bps),
    the input (sell_token, sell_start, sell_end) and the single output
    (buy_token, buy_start, buy_end, receiver); `outputs` replaces the list.
    """

    def _make(**overrides) -> tuple:
        sell_amount = overrides.pop("sell_amount", LOT_AMOUNT)
        buy_amount = overrides.pop("buy_amount", None)
        if buy_amount is None:
            buy_amount = harness.getAmountNeeded(sell_token.address, min(sell_amount, LOT_AMOUNT))
        now = _now()
        fields = {
            "reactor": reactor.address,
            "swapper": harness.address,
            "nonce": NONCE,
            "deadline": harness.frame_end(),
            "validation_contract": ZERO_ADDRESS,
            "validation_data": b"",
            "decay_start": now,
            "decay_end": now,
            "exclusive_filler": ZERO_ADDRESS,
            "override_bps": 0,
            "sell_token": sell_token.address,
            "sell_start": sell_amount,
            "sell_end": sell_amount,
            "buy_token": want.address,
            "buy_start": buy_amount,
            "buy_end": buy_amount,
            "receiver": proceeds_receiver,
        }
        outputs = overrides.pop("outputs", None)
        unknown = set(overrides) - set(fields)
        assert not unknown, unknown
        fields.update(overrides)
        if outputs is None:
            outputs = [
                (fields["buy_token"], fields["buy_start"], fields["buy_end"], fields["receiver"])
            ]
        return (
            (
                fields["reactor"],
                fields["swapper"],
                fields["nonce"],
                fields["deadline"],
                fields["validation_contract"],
                fields["validation_data"],
            ),
            fields["decay_start"],
            fields["decay_end"],
            fields["exclusive_filler"],
            fields["override_bps"],
            (fields["sell_token"], fields["sell_start"], fields["sell_end"]),
            outputs,
        )

    return _make


def _prefix(adapter: Any) -> bytes:
    return bytes.fromhex(str(adapter.address)[2:])


def _bare(order: tuple) -> bytes:
    return encode([ORDER_ABI], [order])


def _signature(adapter: Any, order: tuple) -> bytes:
    """What fillers pass as SignedOrder.sig: Permit2 forwards it verbatim."""
    return _prefix(adapter) + _bare(order)


def _validate(
    harness, adapter, order: tuple, signature: bytes | None = None, domain=DOMAIN_SEPARATOR
):
    signature = _signature(adapter, order) if signature is None else signature
    return harness.isValidSignature(permit_digest_reference(order, domain), signature)


# Deployment pinning


def test_deploy_pins_reactor_permit2_and_domain(adapter, reactor, permit2):
    assert adapter.reactor() == reactor.address
    assert adapter.permit2() == permit2.address
    assert bytes(adapter.domain_separator()) == DOMAIN_SEPARATOR
    assert adapter.ADAPTER_VERSION() == "UniswapXAdapter"


def test_deploy_rejects_zero_reactor(adapter_deployer):
    with boa.reverts(custom_err("BadReactor()")):
        adapter_deployer.deploy(ZERO_ADDRESS)


def test_deploy_rejects_missing_permit2(reactor_deployer, adapter_deployer):
    with boa.reverts(custom_err("BadPermit2()")):
        adapter_deployer.deploy(reactor_deployer.deploy(ZERO_ADDRESS))


def test_deploy_rejects_missing_domain_separator(
    permit2_deployer, reactor_deployer, adapter_deployer
):
    empty_domain = reactor_deployer.deploy(permit2_deployer.deploy(bytes(32)))
    with boa.reverts(custom_err("BadDomainSeparator()")):
        adapter_deployer.deploy(empty_domain)


# Routing: the UniswapX rail is a regular registry adapter behind the prefix


def test_prefixed_bare_order_is_valid_and_unprefixed_is_not(harness, adapter, make_order):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    assert _validate(harness, adapter, order, _bare(order)) == ERC1271_INVALID


def test_disabled_adapter_stops_routing(harness, adapter, make_order, registry, emergency_owner):
    order = make_order()
    with boa.env.prank(emergency_owner):
        registry.disable_adapter(adapter)
    assert _validate(harness, adapter, order) == ERC1271_INVALID


# Transport encoding


def test_malformed_payloads_fail_decoding(harness, adapter, make_order):
    order = make_order()
    # Truncated, empty, or with a second output: not one order at all.
    with boa.reverts():
        _validate(harness, adapter, order, _signature(adapter, order)[:-1])
    with boa.reverts():
        _validate(harness, adapter, order, _prefix(adapter))
    two_outputs = make_order(outputs=[order[6][0], order[6][0]])
    with boa.reverts():
        _validate(harness, adapter, two_outputs)


def test_non_canonical_encodings_rejected(harness, adapter, make_order):
    order = make_order()
    # Decodes to the same order but is not its canonical encoding: trailing
    # bytes, and a padded validation-data tail (the decoder ignores both).
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _signature(adapter, order) + b"\x00")
    bare = _bare(order)
    # The info tuple sits at offset 0x120 of the body; its bytes tail is the
    # 7th word onward. Move the outputs offset by one word and shift the
    # outputs by a zero word so the encoding is loose but still decodes.
    head = bare[: 32 + 9 * 32]
    outputs_offset = int.from_bytes(bare[32 + 8 * 32 : 32 + 9 * 32], "big")
    loose = (
        head[: 32 + 8 * 32]
        + (outputs_offset + 32).to_bytes(32, "big")
        + bare[32 + 9 * 32 : 32 + outputs_offset]
        + bytes(32)
        + bare[32 + outputs_offset :]
    )
    with boa.reverts(custom_err("OrderNotValid(string)", "NonCanonical")):
        _validate(harness, adapter, order, _prefix(adapter) + loose)


# Digest and shape


def test_hash_mismatch_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        harness.isValidSignature(keccak(b"other"), _signature(adapter, order))


def test_foreign_domain_separator_invalid(harness, adapter, make_order):
    order = make_order()
    with boa.reverts(custom_err("OrderNotValid(string)", "InvalidHash")):
        _validate(harness, adapter, order, domain=FOREIGN_DOMAIN)


def test_foreign_reactor_invalid(harness, adapter, make_order):
    other_reactor = boa.env.generate_address("other_reactor")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadReactor")):
        _validate(harness, adapter, make_order(reactor=other_reactor))


def test_foreign_swapper_invalid(harness, adapter, make_order):
    other_swapper = boa.env.generate_address("other_swapper")
    with boa.reverts(custom_err("OrderNotValid(string)", "BadSwapper")):
        _validate(harness, adapter, make_order(swapper=other_swapper))


def test_decaying_input_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "InputDecay")):
        _validate(harness, adapter, make_order(sell_end=LOT_AMOUNT + 1))


def test_no_outputs_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("OrderNotValid(string)", "BadOutputs")):
        _validate(harness, adapter, make_order(outputs=[]))


def test_reactor_rejected_decay_shapes_invalid(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    now = _now()
    with boa.reverts(custom_err("OrderNotValid(string)", "BadDecayDirection")):
        _validate(harness, adapter, make_order(buy_start=quote, buy_end=quote + 1))
    with boa.reverts(custom_err("OrderNotValid(string)", "BadDecayTimes")):
        _validate(
            harness,
            adapter,
            make_order(buy_start=quote + 1, buy_end=quote, decay_start=now, decay_end=now),
        )


def test_validation_hook_and_exclusivity_are_not_the_adapters_concern(harness, adapter, make_order):
    # Both only restrict who may fill or raise what the filler pays; the
    # auction's economics are unaffected, so the adapter passes them through.
    hook = boa.env.generate_address("validation_hook")
    filler = boa.env.generate_address("exclusive_filler")
    order = make_order(
        validation_contract=hook,
        validation_data=b"\x01\x02\x03",
        exclusive_filler=filler,
        override_bps=50,
    )
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE


# Fill-or-kill: the input must be available now


def test_order_above_the_live_remainder_invalid(harness, adapter, make_order, sell_token, want):
    order = make_order()
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    # A native take shrinks the remainder below the order's fixed input; the
    # snapshot-based check_order alone would still accept it.
    taker = boa.env.generate_address("taker")
    taken = LOT_AMOUNT // 4
    want._mint_for_testing(taker, harness.getAmountNeeded(sell_token.address, taken))
    with boa.env.prank(taker):
        want.approve(harness.address, 2**256 - 1)
        harness.take(sell_token.address, taken)
    with boa.reverts(custom_err("OrderNotValid(string)", "ExceedsAvailable")):
        _validate(harness, adapter, order)
    remainder = make_order(sell_amount=LOT_AMOUNT - taken)
    assert _validate(harness, adapter, remainder) == ERC1271_MAGIC_VALUE


# Economics: the reactor-resolved output is what check_order prices


def test_auction_economics_revert_bubbles_through_adapter(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, make_order(buy_amount=quote - 1))
    assert _validate(harness, adapter, make_order(buy_amount=quote + 1)) == ERC1271_MAGIC_VALUE


def test_decaying_output_is_priced_at_its_resolved_amount(harness, adapter, make_order, sell_token):
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    now = _now()
    # Halfway through the decay the reactor resolves the midpoint: at or
    # above the quote passes, below it fails.
    order = make_order(
        buy_start=quote + 20, buy_end=quote - 20, decay_start=now - 10, decay_end=now + 10
    )
    assert _validate(harness, adapter, order) == ERC1271_MAGIC_VALUE
    below = make_order(
        buy_start=quote + 20, buy_end=quote - 22, decay_start=now - 10, decay_end=now + 10
    )
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, below)
    # Past its decay end the order sits at its end amount.
    ended = make_order(
        buy_start=quote + 20, buy_end=quote - 1, decay_start=now - 20, decay_end=now - 10
    )
    with boa.reverts(custom_err("BadBuyAmount()")):
        _validate(harness, adapter, ended)


def test_deadline_past_the_window_invalid(harness, adapter, make_order):
    with boa.reverts(custom_err("BadValidTo()")):
        _validate(harness, adapter, make_order(deadline=harness.frame_end() + 1))


def test_wrong_output_token_or_receiver_invalid(harness, adapter, make_order, sell_token):
    with boa.reverts(custom_err("BadBuyToken()")):
        _validate(harness, adapter, make_order(buy_token=sell_token.address))
    with boa.reverts(custom_err("BadReceiver()")):
        _validate(harness, adapter, make_order(receiver=boa.env.generate_address("thief")))


# Publisher helper: order + signature for the burner


def test_order_for_builds_the_publishable_pair(
    harness, adapter, reactor, sell_token, want, proceeds_receiver, lot
):
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    info, decay_start, decay_end, exclusive_filler, override_bps, input_, outputs = order
    now = _now()
    assert tuple(info) == (
        reactor.address,
        harness.address,
        NONCE,
        harness.frame_end(),
        ZERO_ADDRESS,
        b"",
    )
    assert (decay_start, decay_end) == (now, now)
    assert exclusive_filler == ZERO_ADDRESS
    assert override_bps == 0
    assert tuple(input_) == (sell_token.address, LOT_AMOUNT, LOT_AMOUNT)
    quote = harness.getAmountNeeded(sell_token.address, LOT_AMOUNT)
    assert [tuple(o) for o in outputs] == [(want.address, quote, quote, proceeds_receiver)]
    order = (
        tuple(info),
        decay_start,
        decay_end,
        exclusive_filler,
        override_bps,
        tuple(input_),
        [tuple(o) for o in outputs],
    )
    # The signature is the documented template and validates end-to-end.
    assert bytes(signature) == _signature(adapter, order)
    assert (
        harness.isValidSignature(permit_digest_reference(order, DOMAIN_SEPARATOR), signature)
        == ERC1271_MAGIC_VALUE
    )

    # A partial amount is priced proportionally and validates as well.
    partial, partial_signature = adapter.order_for(
        harness.address, sell_token.address, NONCE + 1, LOT_AMOUNT // 4
    )
    assert tuple(partial[5]) == (sell_token.address, LOT_AMOUNT // 4, LOT_AMOUNT // 4)
    assert partial[6][0][1] == harness.getAmountNeeded(sell_token.address, LOT_AMOUNT // 4)
    assert (
        harness.isValidSignature(
            permit_digest_reference(_to_tuple(partial), DOMAIN_SEPARATOR), partial_signature
        )
        == ERC1271_MAGIC_VALUE
    )


def _to_tuple(order) -> tuple:
    info, decay_start, decay_end, exclusive_filler, override_bps, input_, outputs = order
    return (
        tuple(info),
        decay_start,
        decay_end,
        exclusive_filler,
        override_bps,
        tuple(input_),
        [tuple(o) for o in outputs],
    )


def test_order_for_stays_valid_as_the_curve_decays(harness, adapter, sell_token, lot):
    # A published order is a standing ask: once the curve falls below it, it
    # is still accepted (overpriced for us is fine), so publishers re-post
    # lower with fresh nonces rather than cancel.
    harness.set_frame(_now())
    order, signature = adapter.order_for(harness.address, sell_token.address, NONCE)
    boa.env.time_travel(seconds=10 * STEP_DURATION)
    assert harness.getAmountNeeded(sell_token.address, LOT_AMOUNT) < order[6][0][1]
    assert (
        harness.isValidSignature(
            permit_digest_reference(_to_tuple(order), DOMAIN_SEPARATOR), signature
        )
        == ERC1271_MAGIC_VALUE
    )


def test_order_for_rejects_empty_and_oversized_lots(harness, adapter, sell_token, erc20_deployer):
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, sell_token.address, NONCE)
    fresh = erc20_deployer.deploy("Fresh", "FRESH", 18)
    with boa.reverts(custom_err("NothingToSell()")):
        adapter.order_for(harness.address, fresh.address, NONCE)
    sell_token._mint_for_testing(harness.address, LOT_AMOUNT)
    harness.stage(sell_token.address)
    with boa.reverts(custom_err("ExceedsAvailable()")):
        adapter.order_for(harness.address, sell_token.address, NONCE, LOT_AMOUNT + 1)
