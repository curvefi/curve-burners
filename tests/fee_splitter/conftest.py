from pytest import fixture
import boa


@fixture
def collector():
    return boa.env.generate_address()


@fixture(params=[i for i in range(0, 10001, 2500)])
def collector_weight(request):
    return request.param


@fixture
def incentives_manager():
    return boa.env.generate_address()


@fixture
def owner():
    return boa.env.generate_address()


@fixture
def fee_splitter_deployer():
    from contracts import FeeSplitter
    return FeeSplitter


@fixture()
def mock_factory():
    return boa.load('contracts/testing/ControllerFactoryMock.vy')


@fixture
def mock_controller_deployer():
    return boa.load_partial('contracts/testing/ControllerMock.vy')


@fixture
def fee_splitter(fee_splitter_deployer, target, mock_factory, collector_weight,
                 collector, incentives_manager, owner):
    return fee_splitter_deployer(target, mock_factory, collector_weight,
                                 collector,
                                 incentives_manager, owner)


@fixture
def fee_splitter_with_controllers(fee_splitter, mock_factory,
                                  mock_controller_deployer):
    mock_controllers = [mock_controller_deployer() for _ in range(10)]
    for c in mock_controllers:
        mock_factory.add_controller(c)

    fee_splitter.update_controllers()
    return fee_splitter, mock_controllers
