import boa
import pytest
from eth.exceptions import Revert
from eth_abi import encode
from eth_hash.auto import keccak

from .conftest import custom_err


ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WAD = 10**18
WEEK = 7 * 24 * 60 * 60

START_TOTAL = 100_000 * WAD
FLOOR_TOTAL = WAD
STEP_DURATION = 60
# Reviewed decay bound reused from the dutch auction test suite.
DECAY_FACTOR_RAY = 992031276831159793484252056

STAGED_AMOUNT = 100 * WAD
INTENT_ABI_TYPES = ["uint256", "address", "address", "uint64", "uint256", "uint48"]
TAKE_SELECTOR = keccak(b"take(address,uint256,address,bytes)")[:4]

# AuctionTakerMock payment modes.
PAY_NOTHING = 0
PAY_COLLECTOR = 1
PAY_BURNER = 2

RESOLVER_VERSION = 1
TAKER_RECEIVER_OFFSET = 4 + 2 * 32


def _timestamp() -> int:
    return boa.env.evm.vm.state.timestamp


def _chain_id() -> int:
    return boa.env.evm.patch.chain_id


@pytest.fixture(autouse=True)
def anchor():
    with boa.env.anchor():
        yield


@pytest.fixture(scope="module")
def solver():
    return boa.env.generate_address("solver")


@pytest.fixture(scope="module")
def proceeds_receiver():
    return boa.env.generate_address("proceeds_receiver")


@pytest.fixture(scope="module")
def want():
    return boa.load("contracts/testing/ERC20Mock.vy", "Curve Stablecoin", "crvUSD", 18)


@pytest.fixture(scope="module")
def sell_token():
    return boa.load("contracts/testing/ERC20Mock.vy", "Curve DAO", "CRV", 18)


@pytest.fixture(scope="module")
def auction(want, proceeds_receiver):
    # Deploy shortly after a week boundary so per-test staging and short time
    # travels stay inside the harness auction frame.
    into_next_epoch = WEEK - _timestamp() % WEEK + 3600
    boa.env.time_travel(seconds=into_next_epoch)
    return boa.load(
        "contracts/testing/dutch_auction/CoreHarness.vy",
        want.address,
        proceeds_receiver,
        ZERO_ADDRESS,
        ZERO_ADDRESS,
        START_TOTAL,
        FLOOR_TOTAL,
        DECAY_FACTOR_RAY,
        STEP_DURATION,
    )


@pytest.fixture(scope="module")
def resolver():
    return boa.load("contracts/DutchAuctionResolver.vy")


@pytest.fixture(scope="module")
def taker(auction, proceeds_receiver, want):
    return boa.load(
        "contracts/testing/dutch_auction/AuctionTakerMock.vy",
        auction.address,
        proceeds_receiver,
        want.address,
    )


@pytest.fixture(scope="module")
def stage(auction):
    def _stage(token, amount: int = STAGED_AMOUNT) -> int:
        token._mint_for_testing(auction, amount)
        return auction.stage(token)

    return _stage


@pytest.fixture(scope="module")
def make_payload(auction, sell_token):
    def _make_payload(
        chain_id: int = None,
        auction_address: str = None,
        sell_token_address: str = None,
        auction_epoch: int = None,
        max_sell_amount: int = 2**256 - 1,
        deadline: int = None,
    ) -> bytes:
        if chain_id is None:
            chain_id = _chain_id()
        if auction_address is None:
            auction_address = auction.address
        if sell_token_address is None:
            sell_token_address = sell_token.address
        if auction_epoch is None:
            auction_epoch = auction.current_epoch()
        if deadline is None:
            deadline = _timestamp() + 3600
        return encode(
            INTENT_ABI_TYPES,
            [chain_id, auction_address, sell_token_address, auction_epoch,
             max_sell_amount, deadline],
        )

    return _make_payload


def _with_receiver(call_data: bytes, offset: int, receiver: str) -> bytes:
    word = int(receiver, 16).to_bytes(32, "big")
    return call_data[:offset] + word + call_data[offset + 32:]


def _take_calldata(sell_token_address: str, max_amount: int, receiver: str, data: bytes) -> bytes:
    return TAKE_SELECTOR + encode(
        ["address", "uint256", "address", "bytes"],
        [sell_token_address, max_amount, receiver, data],
    )


# Resolution against live state


def test_resolve_matches_take_quote_in_same_block(auction, resolver, stage, make_payload,
                                                  sell_token, want, proceeds_receiver):
    staged = stage(sell_token)
    resolved = resolver.resolve(make_payload())

    assert resolved.resolver_version == RESOLVER_VERSION
    assert resolved.chain_id == _chain_id()
    assert resolved.auction == auction.address
    assert resolved.auction_epoch == auction.current_epoch()
    assert resolved.quoted_at == _timestamp()

    assert resolved.sell_payout.token == sell_token.address
    assert resolved.sell_payout.amount == staged == auction.available(sell_token)
    # The taker receiver is solver-chosen at fill time, so no recipient is set.
    assert resolved.sell_payout.recipient == ZERO_ADDRESS
    assert resolved.want_payment.token == want.address
    assert resolved.want_payment.amount == auction.getAmountNeeded(sell_token, staged)
    assert resolved.want_payment.amount > 0
    assert resolved.want_payment.recipient == proceeds_receiver


def test_resolve_respects_max_sell_amount(auction, resolver, stage, make_payload, sell_token):
    staged = stage(sell_token)
    partial = staged // 3
    resolved = resolver.resolve(make_payload(max_sell_amount=partial))

    assert resolved.sell_payout.amount == partial
    assert resolved.want_payment.amount == auction.getAmountNeeded(sell_token, partial)


def test_resolve_timing_bounds(auction, resolver, stage, make_payload, sell_token):
    stage(sell_token)
    lot = auction.lots(sell_token)
    lot_start, lot_end = auction.epoch_bounds(lot.epoch)

    # A far deadline is clamped to the last active second of the lot.
    resolved = resolver.resolve(make_payload(deadline=lot_end + WEEK))
    assert resolved.valid_from == lot_start
    assert resolved.fill_deadline == lot_end - 1

    # A near deadline binds tighter than the lot end.
    near_deadline = _timestamp() + 60
    resolved = resolver.resolve(make_payload(deadline=near_deadline))
    assert resolved.fill_deadline == near_deadline


def test_resolved_call_step_is_take_template(auction, resolver, stage, make_payload, sell_token):
    staged = stage(sell_token)
    resolved = resolver.resolve(make_payload())

    assert resolved.call_step.target == auction.address
    assert resolved.call_step.value == 0
    assert resolved.taker_receiver_offset == TAKER_RECEIVER_OFFSET
    # The template leaves the taker receiver zeroed, so it cannot execute
    # unmodified; the solver must write its receiver at the documented offset.
    assert bytes(resolved.call_step.call_data) == _take_calldata(
        sell_token.address, staged, ZERO_ADDRESS, b""
    )


# Solver execution of the resolved order


def test_solver_executes_call_step_and_proceeds_receiver_is_paid(
    auction, resolver, stage, make_payload, sell_token, want, solver, proceeds_receiver
):
    stage(sell_token)
    resolved = resolver.resolve(make_payload())
    payment = resolved.want_payment.amount

    want._mint_for_testing(solver, payment)
    with boa.env.prank(solver):
        want.approve(auction, payment)
    call_data = _with_receiver(
        bytes(resolved.call_step.call_data), resolved.taker_receiver_offset, solver
    )
    boa.env.raw_call(auction.address, sender=solver, data=call_data)

    assert sell_token.balanceOf(solver) == resolved.sell_payout.amount
    assert want.balanceOf(proceeds_receiver) == payment
    assert want.balanceOf(solver) == 0
    assert want.balanceOf(auction) == 0
    assert auction.available(sell_token) == 0


@pytest.mark.parametrize("payment_mode", [PAY_COLLECTOR, PAY_BURNER])
def test_solver_routes_fill_through_callback_aggregator(
    auction, resolver, stage, make_payload, sell_token, want, taker, solver,
    proceeds_receiver, payment_mode
):
    # A solver needing a callback builds its own take() calldata from the
    # resolved amounts, as the ResolvedOrder documentation prescribes. Paying
    # want_payment.recipient (the proceeds receiver) directly or routing the
    # payment through the auction are equally valid: the auction forwards
    # settlement proceeds it receives.
    stage(sell_token)
    resolved = resolver.resolve(make_payload())
    payment = resolved.want_payment.amount

    taker.configure(payment_mode, False)
    want._mint_for_testing(taker, payment)
    call_data = _take_calldata(
        sell_token.address, resolved.sell_payout.amount, taker.address, b"aggregator route"
    )
    boa.env.raw_call(auction.address, sender=solver, data=call_data)

    assert taker.callback_count() == 1
    assert taker.callback_amount_taken() == resolved.sell_payout.amount
    assert taker.callback_amount_needed() == payment
    assert sell_token.balanceOf(taker) == resolved.sell_payout.amount
    assert want.balanceOf(proceeds_receiver) == payment
    assert want.balanceOf(auction) == 0


def test_underpaying_solver_reverts_atomically(
    auction, resolver, stage, make_payload, sell_token, want, solver, proceeds_receiver
):
    staged = stage(sell_token)
    resolved = resolver.resolve(make_payload())

    # The solver holds nothing and approved nothing: the pull for the missing
    # payment fails and the whole fill unwinds.
    call_data = _with_receiver(
        bytes(resolved.call_step.call_data), resolved.taker_receiver_offset, solver
    )
    # boa.env.raw_call surfaces the raw py-evm revert instead of a BoaError.
    with pytest.raises(Revert):
        boa.env.raw_call(auction.address, sender=solver, data=call_data)

    assert sell_token.balanceOf(solver) == 0
    assert want.balanceOf(proceeds_receiver) == 0
    assert auction.available(sell_token) == staged


def test_unmodified_call_step_template_cannot_execute(
    auction, resolver, stage, make_payload, sell_token, solver
):
    stage(sell_token)
    resolved = resolver.resolve(make_payload())
    # take() rejects the zero receiver placeholder left in the template.
    with pytest.raises(Revert) as error:
        boa.env.raw_call(
            auction.address, sender=solver, data=bytes(resolved.call_step.call_data)
        )
    assert error.value.args[0] == keccak(b"ZeroReceiver()")[:4]


def test_fill_at_published_deadline_boundary_succeeds(
    auction, resolver, stage, make_payload, sell_token, want, solver, proceeds_receiver
):
    # The published fill_deadline (lot.end - 1 for a far intent deadline) must
    # itself be fillable: resolve and execute at that exact second.
    stage(sell_token)
    lot = auction.lots(sell_token)
    lot_end = auction.epoch_bounds(lot.epoch)[1]
    payload = make_payload(deadline=lot_end + WEEK)
    boa.env.time_travel(seconds=lot_end - 1 - _timestamp())

    resolved = resolver.resolve(payload)
    assert resolved.fill_deadline == lot_end - 1 == _timestamp()
    payment = resolved.want_payment.amount

    want._mint_for_testing(solver, payment)
    with boa.env.prank(solver):
        want.approve(auction, payment)
    call_data = _with_receiver(
        bytes(resolved.call_step.call_data), resolved.taker_receiver_offset, solver
    )
    boa.env.raw_call(auction.address, sender=solver, data=call_data)

    assert sell_token.balanceOf(solver) == resolved.sell_payout.amount
    assert want.balanceOf(proceeds_receiver) == payment


# Deterministic rejection of invalid intents


def test_expired_intent_reverts(resolver, stage, make_payload, sell_token):
    stage(sell_token)
    with boa.reverts(custom_err("IntentExpired()")):
        resolver.resolve(make_payload(deadline=_timestamp() - 1))
    # The deadline is inclusive: the boundary second still resolves.
    assert resolver.resolve(make_payload(deadline=_timestamp())).sell_payout.amount > 0


def test_wrong_chain_intent_reverts(resolver, stage, make_payload, sell_token):
    stage(sell_token)
    with boa.reverts(custom_err("WrongChain()")):
        resolver.resolve(make_payload(chain_id=_chain_id() + 1))


def test_zero_auction_intent_reverts(resolver, make_payload):
    with boa.reverts(custom_err("BadAuction()")):
        resolver.resolve(make_payload(auction_address=ZERO_ADDRESS))


@pytest.mark.parametrize("epoch_offset", [-1, 1])
def test_wrong_epoch_intent_reverts(auction, resolver, stage, make_payload, sell_token,
                                    epoch_offset):
    stage(sell_token)
    with boa.reverts(custom_err("WrongEpoch()")):
        resolver.resolve(make_payload(auction_epoch=auction.current_epoch() + epoch_offset))


def test_stale_epoch_reverts_after_week_rolls_over(auction, resolver, stage, make_payload,
                                                   sell_token):
    stage(sell_token)
    signed_epoch = auction.current_epoch()
    payload = make_payload(auction_epoch=signed_epoch, deadline=_timestamp() + 2 * WEEK)

    frame_start, frame_end = auction.frame_start(), auction.frame_end()
    auction.set_frame(frame_start + WEEK, frame_end + WEEK)
    boa.env.time_travel(seconds=WEEK)

    assert auction.current_epoch() == signed_epoch + 1
    with boa.reverts(custom_err("WrongEpoch()")):
        resolver.resolve(payload)


def test_unstaged_token_reverts(resolver, make_payload):
    fresh_token = boa.load("contracts/testing/ERC20Mock.vy", "Unstaged", "UNS", 18)
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload(sell_token_address=fresh_token.address))


def test_want_token_intent_reverts(resolver, make_payload, want):
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload(sell_token_address=want.address))


def test_cancelled_lot_reverts(auction, resolver, stage, make_payload, sell_token):
    stage(sell_token)
    auction.cancel(sell_token, auction.current_epoch())
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload())


def test_unsellable_token_reverts(auction, resolver, stage, make_payload, sell_token):
    stage(sell_token)
    auction.set_sellable(sell_token, False)
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload())


def test_lot_past_its_end_reverts(auction, resolver, stage, make_payload, sell_token):
    stage(sell_token)
    lot_end = auction.epoch_bounds(auction.lots(sell_token).epoch)[1]
    boa.env.time_travel(seconds=lot_end - _timestamp())
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload(deadline=_timestamp() + 3600))


def test_drained_lot_reverts(auction, resolver, stage, make_payload, sell_token, want, solver):
    staged = stage(sell_token)
    payment = auction.getAmountNeeded(sell_token, staged)
    want._mint_for_testing(solver, payment)
    with boa.env.prank(solver):
        want.approve(auction, payment)
        auction.take(sell_token, staged, solver, b"")
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload())


def test_zero_max_sell_amount_reverts(resolver, stage, make_payload, sell_token):
    stage(sell_token)
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(make_payload(max_sell_amount=0))


def test_malformed_payload_reverts(resolver, stage, make_payload, sell_token):
    stage(sell_token)
    with boa.reverts():  # dev: truncated payload fails abi decoding
        resolver.resolve(make_payload()[:-32])


# Statelessness


def test_resolver_is_view_and_stateless(resolver, stage, make_payload, sell_token):
    functions = [entry for entry in resolver.abi if entry["type"] == "function"]
    assert functions, "resolver ABI is empty"
    assert all(entry["stateMutability"] in ("view", "pure") for entry in functions)

    stage(sell_token)
    payload = make_payload()
    first = resolver.resolve(payload)
    second = resolver.resolve(payload)
    assert first == second
    # No storage slot was ever written: the resolver works purely off eth_call.
    assert boa.env.evm.get_storage(resolver.address, 0) == 0


def test_newly_staged_token_is_resolvable_without_allowlist(auction, resolver, stage,
                                                            make_payload):
    # Intent discovery needs no DAO allowlist and no resolver registration:
    # a token staged a moment ago resolves immediately.
    fresh_token = boa.load("contracts/testing/ERC20Mock.vy", "Fresh Fee Token", "FRESH", 18)
    payload = make_payload(sell_token_address=fresh_token.address)
    with boa.reverts(custom_err("NothingAvailable()")):
        resolver.resolve(payload)

    staged = stage(fresh_token)
    resolved = resolver.resolve(payload)
    assert resolved.sell_payout.token == fresh_token.address
    assert resolved.sell_payout.amount == staged
    assert resolved.want_payment.amount == auction.getAmountNeeded(fresh_token, staged)
