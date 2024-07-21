import boa
from hypothesis.strategies import composite, just, integers


@composite
def factory(draw):
    return boa.load("contracts/testing/ControllerFactoryMock.vy")


@composite
def fee_splitter_with_factory(draw):
    crvusd = boa.load('contracts/testing/ERC20Mock.vy', "crvusd", "crvusd", 18)

    _factory = draw(factory())
    collector_weight = draw(integers(min_value=0, max_value=10_000))
    collector = draw(just(boa.env.generate_address()))
    incentives_manager = draw(just(boa.env.generate_address()))
    owner = draw(just(boa.env.generate_address()))

    return boa.load('contracts/FeeSplitter.vy', crvusd, _factory,
                    collector_weight, collector, incentives_manager,
                    owner), _factory
