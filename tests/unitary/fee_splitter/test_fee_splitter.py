import boa
import pytest

def test_constructor_expected(fee_splitter_deployer):
    crvusd = boa.env.generate_address()
    factory = boa.env.generate_address()
    _collector = boa.env.generate_address()
    _incentives_manager = boa.env.generate_address()
    _owner = boa.env.generate_address()

    splitter = fee_splitter_deployer(crvusd, factory, 5000, _collector, _incentives_manager, _owner)

    assert splitter._immutables.crvusd == crvusd
    assert splitter._immutables.factory == factory
    assert splitter.collector_weight() == 5000
    assert splitter.collector() == _collector
    assert splitter.incentives_manager() == _incentives_manager
    assert splitter.owner() == _owner

def test_constructor_zero_address(fee_splitter_deployer):
    crvusd = boa.env.generate_address()
    factory = boa.env.generate_address()
    _collector = boa.env.generate_address()
    _incentives_manager = boa.env.generate_address()
    _owner = boa.env.generate_address()

    zero = boa.eval('empty(address)')

    with boa.reverts("zeroaddr: crvusd"):
        fee_splitter_deployer(zero, factory, 5000, _collector, _incentives_manager, _owner)

    with boa.reverts("zeroaddr: factory"):
        fee_splitter_deployer(crvusd, zero, 5000, _collector, _incentives_manager, _owner)

    with boa.reverts("zeroaddr: collector"):
        fee_splitter_deployer(crvusd, factory, 5000, zero, _incentives_manager, _owner)

    with boa.reverts("zeroaddr: incentives_manager"):
        fee_splitter_deployer(crvusd, factory, 5000, _collector, zero, _owner)

    with boa.reverts("zeroaddr: owner"):
        fee_splitter_deployer(crvusd, factory, 5000, _collector, _incentives_manager, zero)

def test_constructor_out_of_bounds(fee_splitter_deployer):
    crvusd = boa.env.generate_address()
    factory = boa.env.generate_address()
    _collector = boa.env.generate_address()
    _incentives_manager = boa.env.generate_address()
    _owner = boa.env.generate_address()

    with boa.reverts("weights: collector_weight > MAX_BPS"):
        fee_splitter_deployer(crvusd, factory, 10001, _collector, _incentives_manager, _owner)


def test_update_controllers_expected(fee_splitter, mock_factory):
    factory = mock_factory
    controllers = []
    N_CONTROLLERS = 3

    def controllers_len():
        """helper to get length of controllers in fee_splitter"""
        return fee_splitter.eval('len(self.controllers)')

    def assert_controllers_match():
        """helper to assert controllers in fee_splitter and factory match"""
        for i in range(controllers_len()):
            assert fee_splitter.controllers(i) == controllers[
                i] == factory.controllers(i)
            assert fee_splitter.allowed_controllers(controllers[i])

    def assert_controllers_length(expected):
        """helper to assert controllers length in fee_splitter"""
        assert controllers_len() == expected == len(controllers)

    # at the start, there should be no controllers
    assert_controllers_length(0)
    assert_controllers_match()

    # we add N_CONTROLLERS controllers to the factory
    for i in range(N_CONTROLLERS):
        controllers.append(c := boa.env.generate_address())
        factory.add_controller(c)

    # we update the controllers in the fee_splitter
    fee_splitter.update_controllers()

    # we make sure that fee_splitter and factory controllers match
    assert_controllers_length(N_CONTROLLERS)
    assert_controllers_match()

    # we add some more controllers to the factory
    for i in range(N_CONTROLLERS):
        controllers.append(c := boa.env.generate_address())
        factory.add_controller(c)

    # we update the controllers in the fee_splitter
    fee_splitter.update_controllers()

    # we make sure that fee_splitter and factory controllers match
    assert_controllers_length(2 * N_CONTROLLERS)
    assert_controllers_match()

    # TODO do stateful testing


def test_claim_controller_fees_expected(fee_splitter_with_controllers, target,
                                        collector, collector_weight,
                                        incentives_manager):
    splitter, _ = fee_splitter_with_controllers
    # TODO fuzz amounts
    crvusd_balance = 10 ** 20
    target.eval(f'self.balanceOf[{splitter.address}] = {crvusd_balance}')

    reported_collector_amount, reported_incentives_amount = (
        splitter.claim_controller_fees())

    collector_amount = crvusd_balance * collector_weight // 10000
    incentives_amount = crvusd_balance - collector_amount

    assert target.balanceOf(splitter) == 0
    assert target.balanceOf(
        collector) == collector_amount == reported_collector_amount
    assert target.balanceOf(
        incentives_manager) == incentives_amount == reported_incentives_amount


def test_claim_controller_fees_all_possibilities(
        fee_splitter_with_controllers):
    splitter, mock_controllers = fee_splitter_with_controllers

    # compute powerset of list_a
    powerset = []
    for i in range(1 << len(mock_controllers)):
        subset = []
        for j in range(len(mock_controllers)):
            if i & (1 << j):
                subset.append(mock_controllers[j])
        powerset.append(subset)

    # remove the empty subset since it be the same as the previous test
    powerset = powerset[1:]

    print("test", mock_controllers)
    # test all claiming possibilities
    for subset in powerset:
        # we reset after every claim to test a new possibility
        with boa.env.anchor():
            splitter.claim_controller_fees(subset)


def test_claim_controller_fees_random_addy(fee_splitter_with_controllers):
    splitter, mock_controllers = fee_splitter_with_controllers

    with boa.reverts("controller: not in factory"):
        splitter.claim_controller_fees([boa.env.generate_address()])


@pytest.mark.parametrize("new_weight", [i for i in range(0, 10001, 500)])
def test_set_weights_expected(fee_splitter, owner, new_weight,
                              collector_weight):
    assert fee_splitter.collector_weight() == collector_weight
    with boa.env.prank(owner):
        fee_splitter.set_weights(new_weight)

    assert fee_splitter.collector_weight() == new_weight


def test_set_weights_unauthorized(fee_splitter, owner):
    with boa.reverts("auth: only owner"):
        fee_splitter.set_weights(1000)


def test_set_weights_out_of_bounds(fee_splitter, owner):
    with boa.reverts("weights: collector weight > MAX_BPS"):
        with boa.env.prank(owner):
            fee_splitter.set_weights(10001)


def test_set_collector_expected(fee_splitter, owner):
    new_collector = boa.env.generate_address()

    assert fee_splitter.collector() != new_collector
    with boa.env.prank(owner):
        fee_splitter.set_collector(new_collector)

    assert fee_splitter.collector() == new_collector


def test_set_collector_unauthorized(fee_splitter, owner):
    with boa.reverts("auth: only owner"):
        fee_splitter.set_collector(boa.env.generate_address())


def test_set_collector_zero_address(fee_splitter, owner):
    zero = boa.eval('empty(address)')

    with boa.reverts("zeroaddr: collector"):
        with boa.env.prank(owner):
            fee_splitter.set_collector(zero)


def test_set_incentives_manager_expected(fee_splitter, owner):
    new_incentives_manager = boa.env.generate_address()

    assert fee_splitter.incentives_manager() != new_incentives_manager
    with boa.env.prank(owner):
        fee_splitter.set_incentives_manager(new_incentives_manager)

    assert fee_splitter.incentives_manager() == new_incentives_manager


def test_set_incentives_manager_unauthorized(fee_splitter, owner):
    with boa.reverts("auth: only owner"):
        fee_splitter.set_incentives_manager(boa.env.generate_address())


def test_set_incentives_manager_zero_address(fee_splitter, owner):
    zero = boa.eval('empty(address)')

    with boa.reverts("zeroaddr: incentives_manager"):
        with boa.env.prank(owner):
            fee_splitter.set_incentives_manager(zero)


def test_set_owner_expected(fee_splitter, owner):
    new_owner = boa.env.generate_address()

    assert fee_splitter.owner() != new_owner
    with boa.env.prank(owner):
        fee_splitter.set_owner(new_owner)

    assert fee_splitter.owner() == new_owner


def test_set_owner_unauthorized(fee_splitter, owner):
    with boa.reverts("auth: only owner"):
        fee_splitter.set_owner(boa.env.generate_address())


def test_set_owner_zero_address(fee_splitter, owner):
    zero = boa.eval('empty(address)')

    with boa.reverts("zeroaddr: new_owner"):
        with boa.env.prank(owner):
            fee_splitter.set_owner(zero)


def test_incentives_weight(fee_splitter, owner, collector_weight):
    assert fee_splitter.incentives_weight() == 10000 - collector_weight

    with boa.env.prank(owner):
        fee_splitter.set_weights(5000)

    assert fee_splitter.incentives_weight() == 5000
