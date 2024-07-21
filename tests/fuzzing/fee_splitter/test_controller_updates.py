import boa
from hypothesis import note
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, \
    initialize
from tests.fuzzing.strategies import fee_splitter_with_factory


class ControllerUpdate(RuleBasedStateMachine):
    @initialize(
        contracts=fee_splitter_with_factory(),
    )
    def setup(self, contracts):
        self.fee_splitter = contracts[0]
        self.factory = contracts[1]
        self.fee_splitter_controllers = []
        self.factory_controllers = []

    @rule()
    def add_controller(self):
        note("[ADD_CONTROLLER]")
        controller = boa.env.generate_address()
        # add it to test list
        self.factory_controllers.append(controller)
        # add it to contract list
        self.factory.add_controller(controller)

    @rule()
    def update_controller(self):
        note("[SYNC_CONTROLLERS]")
        # sync controllers from factory to fee_splitter
        self.fee_splitter.update_controllers()

        self.fee_splitter_controllers = self.factory_controllers[:]
        # check for deep equality of arrays
        assert self.fee_splitter_controllers == self.factory_controllers

    @invariant()
    def controllers_match(self):
        assert self.factory.n_collaterals() >= self.fee_splitter.eval(
            'len(self.controllers)')
        assert self.factory.n_collaterals() == len(self.factory_controllers)
        for i, c in enumerate(self.fee_splitter_controllers):
            assert self.fee_splitter.controllers(i) == c
            assert self.fee_splitter.allowed_controllers(c)

        for i, c in enumerate(self.factory_controllers):
            assert self.factory.controllers(i) == c


TestControllerUpdate = ControllerUpdate.TestCase
