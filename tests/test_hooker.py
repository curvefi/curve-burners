import boa
import pytest

from boa.util.abi import abi_encode
from .conftest import ETH_ADDRESS, ZERO_ADDRESS, WEEK


START_TIME = 1600300800


@pytest.fixture(scope="module", autouse=True)
def preset(fee_collector, target, hooks, hooker, admin, arve, bridger):
    # Transfer from fee_collector to receiver
    target._mint_for_testing(fee_collector, 10 ** 20)
    with boa.env.prank(fee_collector.address):
        target.approve(hooker, 10 ** 19)

    # Transfer from hooker to bridger
    target._mint_for_testing(hooker, 10 ** 17)
    with boa.env.prank(hooker.address):
        target.approve(bridger, 2 ** 256 - 1)

    with boa.env.prank(admin):
        hooker.set_hooks(hooks)

    # Some bridges need gas in the call
    boa.env.set_balance(arve, 10 ** 6)


@pytest.fixture(scope="module")
def bridger():
    return boa.loads("""
#pragma version 0.3.10
from vyper.interfaces import ERC20
distributed: public(bool)
proof: public(Bytes[8192])

@external
@payable
def bridge(coin: ERC20, _receiver: address) -> uint256:
    amount: uint256 = coin.balanceOf(msg.sender)
    coin.transferFrom(msg.sender, self, amount)
    send(_receiver, self.balance)  # Extra after bridge
    return amount
@external
def distribute():
    self.distributed = True
@external
def block_hash_apply(_proof: Bytes[8192]):
    assert len(_proof) == 3
    assert _proof == b"g0d"
    self.proof = _proof
""")


@pytest.fixture(scope="module")
def hooks(bridger, target):
    # Hook: (
    #   to: address,
    #   foreplay: Bytes[8192],  # including method_id
    #   (  # CompensationStrategy
    #       amount: uint256,  # In case of Dutch auction max amount
    #       last_payout_ts: uint256,
    #       start: uint256,
    #       end: uint256,
    #       dutch: bool,
    #   ),
    #   mandatory: bool,
    # )
    return [
        (  # Mandatory
            bridger.address, bridger.bridge.prepare_calldata(target, ZERO_ADDRESS)[:-32],  # omit _receiver
            (0, 0, 0, 0, False), True,
        ),
        (  # Optional
            ZERO_ADDRESS, b"",
            (10 ** 9, 0, 0, 0, False), False,
        ),
        (  # Time frame
            bridger.address, bridger.block_hash_apply.prepare_calldata(b"")[:4],  # only method_id
            (10 ** 9, 0, 1000, 2000, False), False,
        ),
        (  # Overlap time frame
            ZERO_ADDRESS, b"",
            (10 ** 9, 0, 2000, 1000, False), False,
        ),
        (  # Dutch
            ZERO_ADDRESS, b"",
            (10 ** 18, 0, 0, 0, True), False,
        ),
        (  # Dutch with time frame
            bridger.address, bridger.distribute.prepare_calldata(),
            (10 ** 18, 0, 2000, 3000, True), False,
        ),
        (  # Dutch with overlap time
            ZERO_ADDRESS, b"",
            (10 ** 18, 0, 3000, 2000, True), False,
        ),
        (  # Some time in future
            ZERO_ADDRESS, b"",
            (10 ** 3, START_TIME + 2 * WEEK, 0, 0, False), False,
        ),
        (  # Extra mandatory hook
            ZERO_ADDRESS, b"",
            (0, 0, 0, 0, False), True,
        ),
    ]


@pytest.fixture(scope="module")
def hook_inputs(hooks, arve):
    # HookInput: (
    #   hook_id: uint8,
    #   value: uint256,
    #   data: Bytes[8192],
    # )
    datas = {
        0: abi_encode("address", arve),  # bridge
        2: abi_encode("(bytes)", [b"g0d"]),  # block_hash_apply
        5: abi_encode("(bytes)", [b""]),  # distribute
    }
    inputs = [(i, 10 ** 6 if i == 0 else 0, datas.get(i, b"")) for i in range(len(hooks))]

    def inner(*indexes, use_all=False):
        if use_all:
            assert len(indexes) == 0, "Bad input"
            return inputs
        return [inputs[i] for i in sorted(indexes)]
    return inner


def test_set_hook(hooker, hooks, admin, arve):
    for i, hook in enumerate(hooks):
        assert hooker.hooks(i) == hook

    with boa.env.prank(admin):
        with boa.reverts():  # Bad start time
            hooker.set_hooks([(ZERO_ADDRESS, b"", (0, 0, 7 * 24 * 3600, 0, False), False)])
        with boa.reverts():  # Bad end time
            hooker.set_hooks([(ZERO_ADDRESS, b"", (0, 0, 0, 7 * 24 * 3600, False), False)])


def test_compensation(hooker, hook_inputs):
    assert hooker.buffer_amount() == 3 * 10 ** 18 + 3 * 10 ** 9 + 10 ** 3

    ts = START_TIME
    assert hooker.calc_compensation(hook_inputs(0), False, ts) == 0,  "Only free mandatory call"
    assert hooker.calc_compensation(hook_inputs(0), False, ts + 1000) == 0, "Only free mandatory call any time"
    assert hooker.calc_compensation(hook_inputs(0), False, ts + 12345) == 0, "Only free mandatory call any time"

    assert hooker.calc_compensation(hook_inputs(1), False, ts) == 10 ** 9, "Optional not accounted"
    assert hooker.calc_compensation(hook_inputs(1), False, ts + 12345) == 10 ** 9, "Optional not accounted"

    # [1000, 2000)
    assert hooker.calc_compensation(hook_inputs(2), False, ts + 999) == 0, "Time frame not started yet"
    assert hooker.calc_compensation(hook_inputs(2), False, ts + 1000) == 10 ** 9, "Time frame start"
    assert hooker.calc_compensation(hook_inputs(2), False, ts + 1999) == 10 ** 9, "Time frame not ended yet"
    assert hooker.calc_compensation(hook_inputs(2), False, ts + 2000) == 0, "Time frame ended"

    # [2000, 1000)
    assert hooker.calc_compensation(hook_inputs(3), False, ts + 999) == 10 ** 9, "Overlap not ended yet"
    assert hooker.calc_compensation(hook_inputs(3), False, ts + 1000) == 0, "Overlap ended"
    assert hooker.calc_compensation(hook_inputs(3), False, ts + 1999) == 0, "Overlap not started yet"
    assert hooker.calc_compensation(hook_inputs(3), False, ts + 2000) == 10 ** 9, "Overlap started"

    # Dutch
    prev = hooker.calc_compensation(hook_inputs(4), False, ts)
    assert prev <= 10, "Dutch start"
    for dt in range(WEEK // 10, WEEK, WEEK // 10):
        cur = hooker.calc_compensation(hook_inputs(4), False, ts + dt)
        assert prev < cur, "Dutch does not increase"
        prev = cur
    assert prev >= 10 ** 9 - 10, "Dutch end"

    # [2000, 3000)
    assert hooker.calc_compensation(hook_inputs(5), False, ts + 2000) <= 10, "Dutch time frame started"
    assert 10 ** 18 // 2 - 10 <= hooker.calc_compensation(hook_inputs(5), False, ts + 2500) <= 10 ** 18 // 2 + 10,\
        "Dutch time frame in process"
    assert hooker.calc_compensation(hook_inputs(5), False, ts + 3000) == 0, "Dutch time frame ended"

    # [3000, 2000)
    assert hooker.calc_compensation(hook_inputs(6), False, ts + 3000) <= 10, "Dutch overlap started"
    assert 10 ** 18 // 3 <= hooker.calc_compensation(hook_inputs(6), False, ts + WEEK // 2) <= 2 * 10 ** 18 // 3, \
        "Dutch overlap in process"
    assert hooker.calc_compensation(hook_inputs(6), False, ts + 2000) == 0, "Dutch overlap ended"

    assert hooker.calc_compensation(hook_inputs(7), False, ts) == 0, "Time has not come yet"
    assert hooker.calc_compensation(hook_inputs(7), False, ts + 3 * WEEK) == 10 ** 3, "Time has come"

    assert hooker.calc_compensation(hook_inputs(0, 1, 2, 3), False, ts) == 2 * 10 ** 9, "Static don't sum up"
    assert hooker.calc_compensation(hook_inputs(0, 1, 2, 3), False, ts + 1000) == 2 * 10 ** 9, "Static don't sum up"
    assert hooker.calc_compensation(hook_inputs(0, 1, 2, 3), False, ts + 2000) == 2 * 10 ** 9, "Static don't sum up"


def test_act(hooker, hook_inputs, fee_collector, target, bridger, arve, burle):
    with boa.env.prank(arve):
        with boa.env.anchor():
            assert not bridger.distributed()
            assert bridger.proof() == b""

            hooker.act(hook_inputs(use_all=True), value=10 ** 6)

            assert target.balanceOf(hooker) == 0, "Did not transfer"
            assert target.balanceOf(bridger) == 10 ** 17, "Did not transfer"
            assert boa.env.get_balance(arve) == 10 ** 6, "Did not return back"
            assert boa.env.get_balance(bridger.address) == 0, "Did not return back"

            assert bridger.distributed()

            assert bridger.proof() == b"g0d"

        with boa.reverts("Hooks not sorted"):
            hooker.act(list(reversed(hook_inputs(1, 3))))
        with boa.reverts("Hooks not sorted"):
            hooker.calc_compensation(list(reversed(hook_inputs(1, 3))))

        # Receiver
        with boa.env.anchor():
            assert target.balanceOf(burle) == 0
            hooker.act(hook_inputs(1), burle, False)
            assert target.balanceOf(burle) == 10 ** 9
        with boa.env.anchor():
            with boa.env.prank(fee_collector.address):  # FeeCollector pays out itself
                returned = hooker.act(hook_inputs(1), burle, False)
                assert returned == 10 ** 9
                assert target.balanceOf(burle) == 0

        # compensation double spend
        with boa.env.anchor():
            ts_delta = WEEK - (boa.env.evm.vm.state.timestamp - START_TIME) % WEEK
            boa.env.time_travel(seconds=ts_delta + 1000)
            gained = hooker.act(hook_inputs(1, 2, 3))
            assert gained == 2 * 10 ** 9

            boa.env.time_travel(seconds=500)
            gained = hooker.act(hook_inputs(1, 2, 3))
            assert gained == 0

            boa.env.time_travel(seconds=1500)
            gained = hooker.act(hook_inputs(1, 2, 3))
            assert gained == 10 ** 9


def test_mandatory_hooks(hooker, hook_inputs, arve):
    with boa.env.prank(arve):
        with boa.env.anchor():
            hooker.calc_compensation(hook_inputs(0, 8), True)
            hooker.act(hook_inputs(0, 8), arve, True, value=10 ** 6)

        with boa.reverts("Not all mandatory hooks"):
            hooker.calc_compensation(hook_inputs(0), True)
        with boa.reverts("Not all mandatory hooks"):
            hooker.act(hook_inputs(0), arve, True, value=10 ** 6)

    with boa.reverts("Not all mandatory hooks"):
        hooker.calc_compensation(hook_inputs(6, 7, 8), True)
    with boa.reverts("Not all mandatory hooks"):
        hooker.act(hook_inputs(6, 7, 8), arve, True)


def test_one_time_hook(hooker, admin, hooks, burle):
    with boa.env.prank(admin):
        boa.env.set_balance(admin, 10 ** 5)
        hooker.one_time_hooks([hooks[0]], [(0, 10 ** 5, abi_encode("address", burle))], value=10 ** 5)
        assert boa.env.get_balance(burle) == 10 ** 5


def test_admin(hooker, admin, arve):
    # Everything works for admin
    with boa.env.anchor():
        with boa.env.prank(admin):
            hooker.recover([])
            hooker.set_hooks([])
            hooker.one_time_hooks([], [])

    # Third part can not access
    with boa.env.prank(arve):
        with boa.reverts("Only owner"):
            hooker.recover([])
        with boa.reverts("Only owner"):
            hooker.set_hooks([])
        with boa.reverts("Only owner"):
            hooker.one_time_hooks([], [])


def test_emergency_admin(hooker, emergency_admin, arve):
    # Everything works for admin
    with boa.env.anchor():
        with boa.env.prank(emergency_admin):
            hooker.recover([])

    # Third part can not access
    with boa.env.prank(arve):
        with boa.reverts("Only owner"):
            hooker.recover([])


def test_erc165(hooker):
    assert hooker.supportsInterface(bytes.fromhex("01ffc9a7"))


def test_recover_balance(hooker, fee_collector, admin, emergency_admin, arve, weth):
    weth._mint_for_testing(hooker, 10 ** 18)
    boa.env.set_balance(hooker.address, 10 ** 18)

    with boa.env.prank(admin):
        hooker.recover([weth.address, ETH_ADDRESS])

    assert weth.balanceOf(hooker) == 0
    assert boa.env.get_balance(hooker.address) == 0
    assert weth.balanceOf(fee_collector) == 10 ** 18
    assert boa.env.get_balance(fee_collector.address) == 10 ** 18
